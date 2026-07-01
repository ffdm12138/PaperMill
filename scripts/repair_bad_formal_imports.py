"""Audit and repair formal papers polluted by paper_raw resolver side files."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import ALL_CATALOG_PATH, PAPER_NUMBER_LEDGER_PATH, PAPER_RAW_DIR, PAPERS_DIR
from src.naming import safe_child, validate_paper_id
from src.path_utils import normalize_repo_path
from src.services.v2_library import (
    PaperCurationService,
    PaperNumberLedger,
    V2PaperCommitService,
    metadata_doi,
    paper_id_from_metadata_catalog,
    validate_catalog_schema,
    validate_formal_chinese_content,
)
from src.utils.atomic_io import atomic_write_json


_PAPER_NUMBER_RE = re.compile(r"^\d{16}$")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _paper_number(folder: Path) -> str:
    markers = sorted(folder.glob("*.paper.number"))
    if not markers:
        return ""
    try:
        data = json.loads(markers[0].read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return str(data.get("paper_number") or markers[0].stem)


def _resolver_side_file_prefixes(folder: Path) -> list[str]:
    prefixes: list[str] = []
    for path in sorted(folder.glob("*.metadata.candidates.json")) + sorted(folder.glob("*.metadata.resolve_report.json")):
        prefix = path.name.split(".", 1)[0]
        if prefix not in prefixes:
            prefixes.append(prefix)
    return prefixes


def _contains_cjk(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_CJK_RE.search(value))
    if isinstance(value, list):
        return any(_contains_cjk(v) for v in value)
    if isinstance(value, dict):
        return any(_contains_cjk(v) for v in value.values())
    return False


def _candidate_zh(metadata: dict, catalog: dict) -> str:
    title = metadata.get("title") or {}
    for value in (title.get("short_zh"), title.get("translated_zh")):
        if _contains_cjk(value):
            return str(value)
    content_title = ((catalog.get("content_identity") or {}).get("content_title") or "")
    if _contains_cjk(content_title):
        return str(content_title)
    return ""


def _bad_folders(papers_dir: Path) -> list[Path]:
    if not papers_dir.exists():
        return []
    out: list[Path] = []
    for folder in sorted(p for p in papers_dir.iterdir() if p.is_dir()):
        if folder.name == "quarantine":
            continue
        if any(folder.glob("*.metadata.candidates.json")) or any(folder.glob("*.metadata.resolve_report.json")):
            out.append(folder)
    return out


def audit_bad_imports(papers_dir: Path) -> dict:
    items = []
    for folder in _bad_folders(papers_dir):
        old_id = folder.name
        metadata = _read_json(folder / f"{old_id}.metadata.json", {}) or {}
        catalog = _read_json(folder / f"{old_id}.catalog.json", {}) or {}
        issues = ["resolver_side_files_in_formal_library"]
        catalog_errors = validate_catalog_schema(catalog) if catalog else ["missing catalog"]
        if catalog_errors:
            issues.append("catalog_invalid")
        if not _contains_cjk((metadata.get("title") or {}).get("short_zh") or (metadata.get("title") or {}).get("translated_zh") or ""):
            issues.append("missing_confirmed_chinese_title")
        items.append({
            "old_paper_id": old_id,
            "paper_number": _paper_number(folder),
            "resolver_side_file_prefixes": _resolver_side_file_prefixes(folder),
            "doi": metadata_doi(metadata) if metadata else "",
            "current_title": (metadata.get("title") or {}).get("original") or "",
            "candidate_short_zh": _candidate_zh(metadata, catalog),
            "short_zh": _candidate_zh(metadata, catalog),
            "translated_zh": (metadata.get("title") or {}).get("translated_zh") or "",
            "confirmed": False,
            "allow_reimport": True,
            "catalog_path": "",
            "issues": issues,
        })
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "items": items,
    }


def _load_manifest(path: Path) -> list[dict]:
    data = _read_json(path, {})
    if isinstance(data, list):
        return data
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("manifest must be a list or an object with an items list")
    return items


def _validate_manifest_item(item: dict) -> list[str]:
    errors: list[str] = []
    old_id = str(item.get("old_paper_id") or "")
    paper_number = str(item.get("paper_number") or item.get("old_paper_number") or "")
    try:
        validate_paper_id(old_id)
    except Exception as exc:
        errors.append(f"invalid old_paper_id: {exc}")
    if item.get("confirmed") is not True:
        errors.append("confirmed must be true")
    if item.get("allow_reimport", True) is not True:
        errors.append("allow_reimport must be true")
    if not _contains_cjk(str(item.get("short_zh") or item.get("translated_zh") or "")):
        errors.append("short_zh or translated_zh must contain Chinese")
    if not paper_number or not _PAPER_NUMBER_RE.match(paper_number):
        errors.append(f"paper_number must be 16 digits: {paper_number}")
    return errors


def _copy_assets_to_raw(old_folder: Path, raw_folder: Path, old_id: str, item: dict) -> None:
    raw_folder.mkdir(parents=True, exist_ok=False)
    paper_raw_id = raw_folder.name
    metadata = deepcopy(_read_json(old_folder / f"{old_id}.metadata.json", {}) or {})
    metadata["schema_version"] = "1.1"
    metadata["paper_number"] = paper_raw_id
    metadata["paper_raw_id"] = paper_raw_id
    metadata.setdefault("title", {})
    metadata["title"]["short_zh"] = str(item.get("short_zh") or metadata["title"].get("short_zh") or "")
    metadata["title"]["translated_zh"] = str(item.get("translated_zh") or metadata["title"].get("translated_zh") or metadata["title"]["short_zh"])
    atomic_write_json(raw_folder / f"{paper_raw_id}.metadata.json", metadata, indent=2)

    catalog_path = Path(str(item.get("catalog_path") or "")) if item.get("catalog_path") else old_folder / f"{old_id}.catalog.json"
    if not catalog_path.is_absolute():
        catalog_path = Path.cwd() / catalog_path
    shutil.copy2(catalog_path, raw_folder / f"{paper_raw_id}.catalog.json")
    shutil.copy2(old_folder / f"{old_id}.md", raw_folder / f"{paper_raw_id}.md")
    shutil.copy2(old_folder / f"{old_id}.pdf", raw_folder / f"{paper_raw_id}.pdf")
    shutil.copytree(old_folder / "images", raw_folder / "images")
    # The assets were copied from a previously-converted formal folder, so the
    # conversion is already complete. Write a conversion manifest so formalize's
    # conversion gate accepts this paper_raw without re-running MinerU.
    from src.file_fingerprint import compute_sha256
    from src.services.v2_library import _md_sha256_path
    from config.settings import MINERU_BACKEND, MINERU_METHOD, MINERU_LANG, MINERU_EFFORT

    pdf_path = raw_folder / f"{paper_raw_id}.pdf"
    md_path = raw_folder / f"{paper_raw_id}.md"
    images_dir = raw_folder / "images"
    atomic_write_json(raw_folder / f"{paper_raw_id}.conversion.json", {
        "schema_version": "1.0",
        "status": "converted",
        "paper_number": paper_raw_id,
        "paper_raw_id": paper_raw_id,
        "pdf_sha256": compute_sha256(pdf_path),
        "pdf_file_size": pdf_path.stat().st_size,
        "markdown_path": f"{paper_raw_id}.md",
        "markdown_sha256": _md_sha256_path(md_path),
        "images_dir": "images",
        "images_count": sum(1 for p in images_dir.rglob("*") if p.is_file()),
        "backend": MINERU_BACKEND,
        "method": MINERU_METHOD,
        "lang": MINERU_LANG,
        "effort": MINERU_EFFORT,
        "runner": "",
        "api_url": "",
        "output_dir": "",
        "converted_at": datetime.now().isoformat(timespec="seconds"),
    }, indent=2)


def plan_or_apply_repair(
    *,
    manifest_path: Path,
    papers_dir: Path,
    paper_raw_dir: Path,
    all_catalog_path: Path,
    ledger_path: Path,
    apply: bool,
) -> dict:
    items = _load_manifest(manifest_path)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    quarantine_root = papers_dir / "quarantine" / f"formal_bad_imports_{timestamp}"
    results = []
    for item in items:
        old_id = str(item.get("old_paper_id") or "")
        number = str(item.get("paper_number") or item.get("old_paper_number") or "")
        result = {"old_paper_id": old_id, "paper_number": number, "status": "planned", "errors": []}
        errors = _validate_manifest_item(item)
        old_folder = safe_child(papers_dir, old_id) if old_id else papers_dir / "__invalid__"
        raw_folder = safe_child(paper_raw_dir, number) if _PAPER_NUMBER_RE.match(number) else paper_raw_dir / "__invalid__"
        if not old_folder.is_dir():
            errors.append(f"formal folder not found: {old_folder}")
        if raw_folder.exists():
            errors.append(f"paper_raw target already exists: {raw_folder}")
        if errors:
            result.update({"status": "failed", "errors": errors})
            results.append(result)
            continue

        metadata = deepcopy(_read_json(old_folder / f"{old_id}.metadata.json", {}) or {})
        catalog_path = Path(str(item.get("catalog_path") or "")) if item.get("catalog_path") else old_folder / f"{old_id}.catalog.json"
        if not catalog_path.is_absolute():
            catalog_path = Path.cwd() / catalog_path
        catalog = _read_json(catalog_path, {}) or {}
        metadata.setdefault("title", {})
        metadata["title"]["short_zh"] = str(item.get("short_zh") or metadata["title"].get("short_zh") or "")
        metadata["title"]["translated_zh"] = str(item.get("translated_zh") or metadata["title"].get("translated_zh") or metadata["title"]["short_zh"])
        content_errors = validate_catalog_schema(catalog) + validate_formal_chinese_content(metadata, catalog)
        if content_errors:
            result.update({"status": "failed", "errors": content_errors})
            results.append(result)
            continue
        result["new_paper_id"] = paper_id_from_metadata_catalog(metadata, catalog)
        result["quarantine_dir"] = normalize_repo_path(quarantine_root / old_id)
        result["paper_raw_dir"] = normalize_repo_path(raw_folder)
        if not apply:
            results.append(result)
            continue

        quarantine_root.mkdir(parents=True, exist_ok=True)
        q_folder = quarantine_root / old_id
        shutil.move(str(old_folder), q_folder)
        try:
            _copy_assets_to_raw(q_folder, raw_folder, old_id, item)
            PaperNumberLedger(ledger_path).reserve_specific_for_paper_raw(number, raw_folder)
            curation = PaperCurationService().apply_curated_files(raw_folder)
            if not curation.get("success"):
                raise RuntimeError("; ".join(curation.get("errors") or [curation.get("status", "curation failed")]))
            from src.services.paper_raw_formalizer import PaperRawFormalizationService

            formalized = PaperRawFormalizationService(
                paper_raw_dir=Path(raw_folder).parent,
                papers_dir=papers_dir,
                all_catalog_path=all_catalog_path,
                ledger_path=ledger_path,
            ).formalize(Path(curation["folder"]), preserve_paper_number=number or None)
            if not formalized.get("success"):
                raise RuntimeError("; ".join(formalized.get("errors") or [formalized.get("status", "formalize failed")]))
            committed = V2PaperCommitService(
                papers_dir=papers_dir,
                all_catalog_path=all_catalog_path,
                ledger_path=ledger_path,
            ).commit_paper_raw(Path(formalized["folder"]))
            if committed.get("status") != "imported":
                raise RuntimeError("; ".join(committed.get("errors") or [committed.get("status", "commit failed")]))
            result.update(committed)
        except Exception as exc:
            result.update({"status": "failed", "errors": [str(exc)]})
        results.append(result)
    return {"applied": apply, "quarantine_root": normalize_repo_path(quarantine_root), "items": results}


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit or repair bad formal imports containing resolver side files.")
    parser.add_argument("--audit", action="store_true", help="emit a repair manifest template")
    parser.add_argument("--manifest", type=Path, default=None, help="confirmed repair manifest")
    parser.add_argument("--apply", action="store_true", help="perform the repair; default is dry-run")
    parser.add_argument("--dry-run", action="store_true", help="force dry-run")
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--all-catalog-path", type=Path, default=Path(ALL_CATALOG_PATH))
    parser.add_argument("--ledger-path", type=Path, default=Path(PAPER_NUMBER_LEDGER_PATH))
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    if args.audit or not args.manifest:
        report = audit_bad_imports(args.papers_dir)
    else:
        report = plan_or_apply_repair(
            manifest_path=args.manifest,
            papers_dir=args.papers_dir,
            paper_raw_dir=args.paper_raw_dir,
            all_catalog_path=args.all_catalog_path,
            ledger_path=args.ledger_path,
            apply=args.apply and not args.dry_run,
        )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    items = report.get("items", [])
    return 1 if any(item.get("status") == "failed" for item in items) else 0


if __name__ == "__main__":
    raise SystemExit(main())
