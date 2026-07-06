"""Validate v2 paper library structure.

DESIGN SCOPE
------------
This validator is designed for the **real local library** (``data/papers/``) with
complete assets — PDFs, images, .paper.number markers, asset manifests, and
full catalog/metadata. It enforces strict structural, schema, and content
constraints required for the committed library.

It is NOT designed for the audit snapshot zip (``mineru_snapshot.zip``).
The audit zip may contain only partial runtime samples (e.g. only .json/.md
without PDF/images), and this validator will correctly flag those as
incomplete / broken — that is expected behavior.

If you need to validate a partial dataset (e.g. a snapshot extracted from
the audit zip), consider using ``--mode snapshot-audit`` (future) or running
only the sub-components (schema checks, catalog refs) that do not require
physical PDF/image presence.

See ``AGENTS.md`` §7 for the two-layer contract (git hygiene vs audit snapshot).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import ALL_CATALOG_PATH, PAPERS_DIR
from src.file_fingerprint import compute_file_hashes
from src.path_utils import resolve_stored_path
from src.services.catalog_asset_refs import validate_catalog_asset_refs
from src.services.asset_manifest import pdf_hashes_from_manifest, read_asset_manifest
from src.services.source_records import validate_metadata_source_record_exists
from src.services.v2_library import (
    ALL_CATALOG_SCHEMA_VERSION,
    _formal_assets_found,
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
    if data.get("paper_number"):
        return str(data.get("paper_number") or "")
    if marker.name.endswith(".paper.number"):
        return marker.name[: -len(".paper.number")]
    return str(marker.stem)


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
    locator = catalog.get("library_locator") if isinstance(catalog.get("library_locator"), dict) else {}
    if locator.get("paper_id") != pid:
        errors.append(f"{pid} catalog.library_locator.paper_id must equal folder name")
    effective_number = paper_number or str(locator.get("paper_number") or "")
    if not paper_number:
        errors.append(f"{pid} missing paper_number marker")
    elif locator.get("paper_number") != paper_number:
        errors.append(f"{pid} catalog.library_locator.paper_number must equal ledger/marker paper_number")
    effective_number = paper_number or str(locator.get("paper_number") or "")
    asset_refs = locator.get("asset_refs") if isinstance(locator.get("asset_refs"), dict) else {}
    for field in ("markdown", "pdf", "metadata", "catalog", "asset_manifest", "images_dir"):
        value = str(asset_refs.get(field) or "")
        if not value:
            errors.append(f"{pid} catalog.library_locator.asset_refs.{field} missing")
        elif not _asset_ref_exists(folder, value):
            errors.append(f"{pid} catalog.library_locator.asset_refs.{field} does not exist: {value}")
    if effective_number:
        for err in validate_catalog_asset_refs(
            folder,
            catalog,
            paper_number=effective_number,
            paper_id=pid,
            stage="papers",
        ):
            errors.append(f"{pid} {err}")
    return errors


def _formal_asset_manifest_errors(pid: str, folder: Path, paper_number: str) -> list[str]:
    errors: list[str] = []
    manifest_path = folder / f"{pid}.asset_manifest.json"
    # 最后防线：扫描全部 *.asset_manifest.json，拒绝 paper_raw 阶段残留的额外 manifest。
    manifest_files = list(folder.glob("*.asset_manifest.json"))
    extras = [f for f in manifest_files if f != manifest_path]
    for f in extras:
        errors.append(
            f"{pid}: unexpected asset manifest in formal paper directory: {f.name}; "
            f"expected only {pid}.asset_manifest.json"
        )
    if not manifest_path.exists():
        return [f"{pid} missing asset_manifest: {manifest_path}"] + errors
    manifest = read_asset_manifest(folder, pid)
    if not manifest:
        return [f"{pid} asset_manifest invalid or empty: {manifest_path}"] + errors
    if str(manifest.get("schema_version") or "") != "1.0":
        errors.append(f"{pid} asset_manifest.schema_version must be 1.0")
    if str(manifest.get("paper_number") or "") != paper_number:
        errors.append(f"{pid} asset_manifest.paper_number must equal marker number")
    if str(manifest.get("paper_id") or "") != pid:
        errors.append(f"{pid} asset_manifest.paper_id must equal folder name")
    if str(manifest.get("stage") or "") != "papers":
        errors.append(f"{pid} asset_manifest.stage must be papers")
    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    expected = {
        "pdf": f"{pid}.pdf",
        "markdown": f"{pid}.md",
        "metadata": f"{pid}.metadata.json",
        "catalog": f"{pid}.catalog.json",
    }
    for key, rel_path in expected.items():
        entry = files.get(key)
        if not isinstance(entry, dict):
            errors.append(f"{pid} asset_manifest.files.{key} missing")
            continue
        if str(entry.get("path") or "") != rel_path:
            errors.append(f"{pid} asset_manifest.files.{key}.path must be {rel_path}")
        actual_path = folder / rel_path
        if not actual_path.exists():
            errors.append(f"{pid} asset_manifest.files.{key}.path does not exist: {rel_path}")
            continue
        if key in {"pdf", "markdown"}:
            try:
                actual = compute_file_hashes(actual_path)
            except OSError as exc:
                errors.append(f"{pid} asset_manifest.files.{key} cannot hash {rel_path}: {exc}")
                continue
            for hash_key in ("sha256", "file_size"):
                if hash_key not in entry:
                    errors.append(f"{pid} asset_manifest.files.{key}.{hash_key} missing")
                elif str(entry.get(hash_key)) != str(actual.get(hash_key)):
                    errors.append(f"{pid} asset_manifest.files.{key}.{hash_key} does not match actual file")
            if key == "pdf":
                if "md5" not in entry:
                    errors.append(f"{pid} asset_manifest.files.pdf.md5 missing")
                elif str(entry.get("md5") or "").lower() != str(actual.get("md5") or "").lower():
                    errors.append(f"{pid} asset_manifest.files.pdf.md5 does not match actual file")
    images_dir = files.get("images_dir")
    if images_dir != "images/":
        errors.append(f"{pid} asset_manifest.files.images_dir must be images/")
    elif not (folder / "images").is_dir():
        errors.append(f"{pid} asset_manifest.files.images_dir does not exist: images/")
    return errors


def _formal_ledger_errors(
    *,
    ledger: PaperNumberLedger,
    papers_dir: Path,
    formal_paper_numbers: set[str],
    paper_id_to_number: dict[str, str],
) -> list[str]:
    errors: list[str] = []
    data = ledger.load()
    items = data.get("items") or {}
    active_numbers = {number for number, item in items.items() if (item or {}).get("state", "active") == "active"}
    if formal_paper_numbers != active_numbers:
        errors.append(
            "active ledger paper_number set must equal data/papers markers "
            f"(active={sorted(active_numbers)} markers={sorted(formal_paper_numbers)})"
        )
    for pid, number in sorted(paper_id_to_number.items()):
        item = items.get(number)
        folder = papers_dir / pid
        if not item:
            errors.append(f"{pid} marker paper_number {number} missing from ledger")
            continue
        state = item.get("state") or "active"
        if state != "active":
            errors.append(f"{pid} ledger state for marker {number} must be active, got {state}")
        stored = item.get("folder_path") or ""
        resolved = resolve_stored_path(stored)
        try:
            same = resolved.resolve() == folder.resolve()
        except OSError:
            same = False
        if stored and not same:
            errors.append(f"{pid} ledger folder_path for {number} must point to formal folder")
    for number in sorted(active_numbers):
        item = items.get(number) or {}
        folder = resolve_stored_path(item.get("folder_path") or "")
        if not folder.exists():
            # PaperNumberLedger.validate also reports this; keep the specific
            # formal invariant here for callers that only inspect this validator.
            errors.append(f"active ledger folder missing: {number} {folder}")
            continue
        marker = folder / f"{number}.paper.number"
        if not marker.exists():
            errors.append(f"active ledger folder missing marker: {number} {folder}")
            continue
        marker_number = _paper_number_from_markers([marker])
        if marker_number != number:
            errors.append(f"active ledger marker mismatch: {number} vs {marker_number} in {folder}")
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
    ledger = PaperNumberLedger(ledger_path)
    ledger_errors, ledger_warnings = ledger.validate(papers_dir)
    errors.extend(ledger_errors)
    warnings.extend(ledger_warnings)

    formal_paper_ids: set[str] = set()
    formal_paper_numbers: set[str] = set()
    paper_id_to_number: dict[str, str] = {}
    doi_to_paper_ids: dict[str, list[str]] = {}
    pdf_sha_to_paper_ids: dict[str, list[str]] = {}
    pdf_md5_to_paper_ids: dict[str, list[str]] = {}
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
            # 任意正式资产（含 stale numbered manifest、marker-only、images 文件）都触发正式库校验。
            has_any_v2_asset = bool(_formal_assets_found(folder, pid))
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
            if len(markers) != 1:
                errors.append(f"{pid}: formal paper directory must have exactly one *.paper.number marker, found {len(markers)}")
            paper_number = _paper_number_from_markers(markers)
            if paper_number:
                formal_paper_numbers.add(paper_number)
                paper_id_to_number[pid] = paper_number
            # images 必须是目录，不能只是同名文件。
            images_path = required["images"]
            if images_path.exists() and not images_path.is_dir():
                errors.append(f"{pid}: images must be a directory, not a file: {images_path}")
            for name, path in required.items():
                if not path.exists():
                    errors.append(f"{pid} missing {name}: {path}")
            # 必须有正式 <pid>.asset_manifest.json（缺则 _formal_asset_manifest_errors 已报）。
            # stale numbered manifest-only 目录此时会被 1) glob extras、2) required missing 一起报错。
            errors.extend(_formal_asset_manifest_errors(pid, folder, paper_number))
            metadata = {}
            if required["metadata"].exists():
                metadata = _load_json_or_error(required["metadata"], pid, errors)
                if metadata:
                    errors.extend([f"{pid} {err}" for err in validate_metadata_schema(metadata)])
                    errors.extend([f"{pid} {err}" for err in validate_metadata_source_record_exists(
                        required["metadata"].parent,
                        (metadata.get("source") or {}).get("raw_record_path", ""),
                        require_nonempty=True,
                    )])
                    errors.extend(_formal_metadata_errors(pid, metadata))
                    warnings.extend([f"{pid} {warning}" for warning in metadata_reference_warnings_for_commit(metadata)])
                    doi = metadata_doi(metadata)
                    if doi:
                        doi_to_paper_ids.setdefault(doi, []).append(pid)
                    pdf_md5, pdf_sha = pdf_hashes_from_manifest(folder, pid)
                    if required["pdf"].exists():
                        try:
                            hashes = compute_file_hashes(required["pdf"])
                            pdf_sha = str(hashes.get("sha256") or pdf_sha).lower()
                            pdf_md5 = str(hashes.get("md5") or pdf_md5).lower()
                        except OSError:
                            pass
                    if pdf_sha:
                        pdf_sha_to_paper_ids.setdefault(pdf_sha, []).append(pid)
                    if pdf_md5:
                        pdf_md5_to_paper_ids.setdefault(pdf_md5, []).append(pid)
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
    for pdf_sha, paper_ids in sorted(pdf_sha_to_paper_ids.items()):
        if len(paper_ids) > 1:
            errors.append(f"duplicate PDF sha256 in formal library: {pdf_sha} ({', '.join(sorted(paper_ids))})")
    for pdf_md5, paper_ids in sorted(pdf_md5_to_paper_ids.items()):
        if len(paper_ids) > 1:
            errors.append(f"duplicate PDF md5 in formal library: {pdf_md5} ({', '.join(sorted(paper_ids))})")
    errors.extend(_formal_ledger_errors(
        ledger=ledger,
        papers_dir=papers_dir,
        formal_paper_numbers=formal_paper_numbers,
        paper_id_to_number=paper_id_to_number,
    ))

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
    if str(data.get("schema_version") or "") != ALL_CATALOG_SCHEMA_VERSION:
        errors.append(f"all.catalog.schema_version must be {ALL_CATALOG_SCHEMA_VERSION}")
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
    if formal_paper_ids and seen_ids != formal_paper_ids:
        errors.append(
            "all.catalog paper_id set must equal data/papers folders "
            f"(catalog={sorted(seen_ids)} folders={sorted(formal_paper_ids)})"
        )
    if formal_paper_numbers and seen_numbers != formal_paper_numbers:
        errors.append(
            "all.catalog paper_number set must equal data/papers markers "
            f"(catalog={sorted(seen_numbers)} markers={sorted(formal_paper_numbers)})"
        )

    # paper_index.json: path mapping only, no bibliographic fields
    index_path = all_catalog_path.parent / "paper_index.json"
    if formal_paper_ids and not index_path.exists():
        errors.append(f"missing paper_index.json: {index_path}")
    if index_path.exists():
        index_data = _load_json_or_error(index_path, "paper_index", errors)
        if str(index_data.get("schema_version") or "") != "2.0":
            errors.append("paper_index.schema_version must be 2.0")
        index_ids: set[str] = set()
        index_numbers: set[str] = set()
        for i, item in enumerate(index_data.get("papers", [])):
            ctx = f"paper_index[{i}]"
            if isinstance(item, dict):
                if item.get("paper_id"):
                    index_ids.add(str(item.get("paper_id")))
                if item.get("paper_number"):
                    index_numbers.add(str(item.get("paper_number")))
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
        if formal_paper_ids and index_ids != formal_paper_ids:
            errors.append(
                "paper_index paper_id set must equal data/papers folders "
                f"(index={sorted(index_ids)} folders={sorted(formal_paper_ids)})"
            )
        if formal_paper_numbers and index_numbers != formal_paper_numbers:
            errors.append(
                "paper_index paper_number set must equal data/papers markers "
                f"(index={sorted(index_numbers)} markers={sorted(formal_paper_numbers)})"
            )
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
