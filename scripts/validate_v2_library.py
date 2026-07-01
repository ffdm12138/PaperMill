"""Validate v2 paper library structure."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import ALL_CATALOG_PATH, PAPERS_DIR
from src.path_utils import resolve_stored_path
from src.services.v2_library import (
    find_forbidden_catalog_keys,
    find_legacy_all_catalog_entry_keys,
    PaperNumberLedger,
    metadata_reference_warnings_for_commit,
    metadata_doi,
    validate_all_catalog_entry,
    validate_catalog_schema,
    validate_formal_chinese_content,
    validate_metadata_completeness_for_commit,
    validate_metadata_schema,
)


def _formal_metadata_errors(ctx: str, metadata: dict) -> list[str]:
    errors = []
    for err in validate_metadata_completeness_for_commit(metadata):
        if err == "metadata.identifiers.doi is required for formal commit":
            errors.append(f"{ctx} metadata.identifiers.doi is required in formal library")
        else:
            errors.append(f"{ctx} {err}")
    return errors


def _paper_number_from_markers(markers: list[Path]) -> str:
    if not markers:
        return ""
    marker = markers[0]
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return str(data.get("paper_number") or marker.stem)


def _load_json_or_error(path: Path, ctx: str, errors: list[str]) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(f"{ctx} invalid JSON at {path}: {exc}")
        return {}
    if not isinstance(data, dict):
        errors.append(f"{ctx} JSON must be an object: {path}")
        return {}
    return data


def _asset_ref_exists(folder: Path, value: str) -> bool:
    if not value:
        return False
    path = Path(value)
    if not path.is_absolute():
        path = folder / value
    return path.exists()


def _formal_catalog_errors(pid: str, folder: Path, catalog: dict, paper_number: str) -> list[str]:
    errors: list[str] = []
    if catalog.get("paper_id") != pid:
        errors.append(f"{pid} catalog.paper_id must equal folder name")
    if not paper_number:
        errors.append(f"{pid} missing paper_number marker")
    elif catalog.get("paper_number") != paper_number:
        errors.append(f"{pid} catalog.paper_number must equal ledger/marker paper_number")
    asset_refs = catalog.get("asset_refs") or {}
    for field in ("markdown", "pdf", "metadata", "catalog", "images_dir"):
        value = str(asset_refs.get(field) or "")
        if not value:
            errors.append(f"{pid} catalog.asset_refs.{field} missing")
        elif not _asset_ref_exists(folder, value):
            errors.append(f"{pid} catalog.asset_refs.{field} does not exist: {value}")
    return errors


def validate_v2_library(
    *,
    papers_dir: Path = PAPERS_DIR,
    all_catalog_path: Path = ALL_CATALOG_PATH,
    check_paths: bool = True,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    ledger_path = all_catalog_path.parent / "paper_number_ledger.json"
    ledger_errors, ledger_warnings = PaperNumberLedger(ledger_path).validate(papers_dir)
    errors.extend(ledger_errors)
    warnings.extend(ledger_warnings)

    formal_paper_ids: set[str] = set()
    doi_to_paper_ids: dict[str, list[str]] = {}
    if papers_dir.exists():
        for folder in sorted(p for p in papers_dir.iterdir() if p.is_dir()):
            pid = folder.name
            required = {
                "metadata": folder / f"{pid}.metadata.json",
                "catalog": folder / f"{pid}.catalog.json",
                "md": folder / f"{pid}.md",
                "pdf": folder / f"{pid}.pdf",
                "images": folder / "images",
            }
            has_any_v2_asset = any(path.exists() for path in required.values())
            if (folder / "paper.md").exists():
                errors.append(f"{pid}: formal v2 library must not contain paper.md")
            if (folder / "output").exists():
                errors.append(f"{pid}: MinerU raw output must be removed before commit (delete output/)")
            for pattern in (
                "*.metadata.candidates.json",
                "*.metadata.resolve_report.json",
                "*.metadata.patch.json",
                "*.conversion.json",
                "*.formalization.json",
            ):
                for vestige in folder.glob(pattern):
                    errors.append(f"{pid}: paper_raw transient file must not enter formal library: {vestige.name}")
            for vestige in folder.glob("*.tmp"):
                errors.append(f"{pid}: temporary file must not enter formal library: {vestige.name}")
            if (folder / "curation_prompt.md").exists():
                errors.append(f"{pid}: curation prompt must not enter formal library")
            if (folder / ".import_status.json").exists():
                errors.append(f"{pid}: import_status marker must not enter formal library")
            if (folder / "stage_manifest.json").exists():
                errors.append(f"{pid}: paper_raw stage manifest must not enter formal library")
            if not has_any_v2_asset:
                continue
            formal_paper_ids.add(pid)
            markers = list(folder.glob("*.paper.number"))
            paper_number = _paper_number_from_markers(markers)
            for name, path in required.items():
                if not path.exists():
                    errors.append(f"{pid} missing {name}: {path}")
            metadata = {}
            if required["metadata"].exists():
                metadata = _load_json_or_error(required["metadata"], pid, errors)
                if metadata:
                    errors.extend([f"{pid} {err}" for err in validate_metadata_schema(metadata)])
                    errors.extend(_formal_metadata_errors(pid, metadata))
                    warnings.extend([f"{pid} {warning}" for warning in metadata_reference_warnings_for_commit(metadata)])
                    doi = metadata_doi(metadata)
                    if doi:
                        doi_to_paper_ids.setdefault(doi, []).append(pid)
            if required["catalog"].exists():
                catalog = _load_json_or_error(required["catalog"], pid, errors)
                if catalog:
                    errors.extend([f"{pid} {err}" for err in validate_catalog_schema(catalog)])
                    errors.extend(_formal_catalog_errors(pid, folder, catalog, paper_number))
                    if metadata:
                        errors.extend([f"{pid} {err}" for err in validate_formal_chinese_content(metadata, catalog)])
            if len(markers) > 1:
                errors.append(f"{pid} has multiple .paper.number files")

    for doi, paper_ids in sorted(doi_to_paper_ids.items()):
        if len(paper_ids) > 1:
            errors.append(f"duplicate metadata.identifiers.doi in formal library: {doi} ({', '.join(sorted(paper_ids))})")

    if not all_catalog_path.exists():
        if formal_paper_ids:
            errors.append(f"missing all.catalog.json: {all_catalog_path}")
        return errors, warnings

    data = _load_json_or_error(all_catalog_path, "all.catalog", errors)
    if not data:
        return errors, warnings
    if not isinstance(data, dict):
        errors.append("all.catalog must be an object")
        return errors, warnings
    if str(data.get("schema_version") or "") != "2.0":
        errors.append("all.catalog.schema_version must be 2.0")
    seen_numbers: set[str] = set()
    seen_ids: set[str] = set()
    for i, entry in enumerate(data.get("papers", [])):
        ctx = f"papers[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{ctx} all.catalog entry must be an object")
            continue
        number = entry.get("paper_number") or ""
        pid = entry.get("paper_id") or ""
        if not number:
            errors.append(f"{ctx} missing paper_number")
        elif number in seen_numbers:
            errors.append(f"{ctx} duplicate paper_number: {number}")
        seen_numbers.add(number)
        if not pid:
            errors.append(f"{ctx} missing paper_id")
        elif pid in seen_ids:
            errors.append(f"{ctx} duplicate paper_id: {pid}")
        seen_ids.add(pid)
        # all.catalog entries are content-only; ensure no forbidden bibliographic
        # keys leaked in (full catalog-schema validation happens on the on-disk
        # <pid>.catalog.json above; all.catalog entries omit schema_version/provenance).
        for err in validate_all_catalog_entry(entry):
            errors.append(f"{ctx} {err}")
        for k in find_legacy_all_catalog_entry_keys(entry):
            errors.append(f"{ctx} all.catalog entry must not contain legacy wrapper/path key: {k}")
        if "metadata" in entry:
            errors.append(f"{ctx} all.catalog entry must not embed metadata (read metadata.json by paper_number)")
        if check_paths:
            asset_refs = entry.get("asset_refs") or {}
            for field in ("markdown", "pdf", "images_dir"):
                value = asset_refs.get(field) or ""
                if not value:
                    errors.append(f"{ctx} missing asset_refs.{field}")
                    continue
                if not resolve_stored_path(value).exists():
                    errors.append(f"{ctx} asset_refs.{field} does not exist: {value}")

    # paper_index.json: path mapping only, no bibliographic fields
    index_path = all_catalog_path.parent / "paper_index.json"
    if formal_paper_ids and not index_path.exists():
        errors.append(f"missing paper_index.json: {index_path}")
    if index_path.exists():
        index_data = _load_json_or_error(index_path, "paper_index", errors)
        for i, item in enumerate(index_data.get("papers", [])):
            ctx = f"paper_index[{i}]"
            forbidden = find_forbidden_catalog_keys(item)
            for k in forbidden:
                errors.append(f"{ctx} forbidden bibliographic key in paper_index: {k}")
            if check_paths:
                for field in ("metadata_path", "catalog_path", "markdown_path", "pdf_path", "images_dir"):
                    value = item.get(field) or ""
                    if not value:
                        errors.append(f"{ctx} missing {field}")
                        continue
                    if not resolve_stored_path(value).exists():
                        errors.append(f"{ctx} {field} does not exist: {value}")
    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate v2 library structure.")
    parser.add_argument("--no-check-paths", action="store_true", help="do not require local paper assets to exist")
    args = parser.parse_args()
    errors, warnings = validate_v2_library(check_paths=not args.no_check_paths)
    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}")
    print(f"valid={not errors} errors={len(errors)} warnings={len(warnings)}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
