"""LEGACY-ONLY AUDIT SCRIPT.

Audit formal papers imported from legacy paper_raw layouts and optionally delete
safe bad copies.

Also folds in the formal-ingest state audit: each formal paper is checked for
marker presence, paper_number consistency, metadata completeness/match,
catalog content-only, asset_refs existence, transient files, suspicious
paper_id, and duplicate DOI/pdf-sha/md-sha. With ``--quarantine --apply`` the
clearly-bad entries are moved to ``data/papers_quarantine/`` and the catalog
indexes are rebuilt (paper_number is never reused — the ledger keeps the
number, flagged via warning).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import ALL_CATALOG_PATH, PAPER_NUMBER_LEDGER_PATH, PAPER_RAW_DIR, PAPERS_DIR
from src.file_fingerprint import compute_sha256
from src.services.v2_library import (
    AllCatalogBuilder,
    PaperNumberLedger,
    assess_paper_raw_commit_readiness,
    find_forbidden_catalog_keys,
    metadata_doi,
    metadata_is_matched,
    now_iso,
    validate_catalog_schema,
    validate_metadata_completeness_for_commit,
)

_TRANSIENT_PATTERNS = (
    "stage_manifest.json",
    "*.conversion.json",
    "*.metadata.patch.json",
    "*.metadata.candidates.json",
    "*.metadata.resolve_report.json",
    "curation_prompt.md",
    "*.formalization.json",
)
_SUSPICIOUS_PAPER_ID_HINTS = ("download", "article", "fulltext", "doi", "10.")
_PAPER_ID_STRUCTURE_RE = re.compile(r"^\d{4}_[A-Za-z].*_")


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _sha(path: Path) -> str:
    try:
        return compute_sha256(path)
    except OSError:
        return ""


def _raw_backing_index(paper_raw_dir: Path) -> dict:
    by_source: dict[str, list[str]] = {}
    by_pdf_sha: dict[str, list[str]] = {}
    by_md_sha: dict[str, list[str]] = {}
    if not paper_raw_dir.exists():
        return {"source_id": by_source, "pdf_sha256": by_pdf_sha, "markdown_sha256": by_md_sha}
    for folder in sorted(p for p in paper_raw_dir.rglob("*") if p.is_dir()):
        source_id = folder.name if folder.name.isdigit() and len(folder.name) == 6 else ""
        for meta_path in folder.glob("*.metadata.json"):
            source_id = str((_load_json(meta_path).get("source_id") or source_id))
        if source_id:
            by_source.setdefault(source_id, []).append(str(folder))
        for pdf in folder.glob("*.pdf"):
            sha = _sha(pdf)
            if sha:
                by_pdf_sha.setdefault(sha, []).append(str(folder))
        for md in folder.glob("*.md"):
            sha = _sha(md)
            if sha:
                by_md_sha.setdefault(sha, []).append(str(folder))
    return {"source_id": by_source, "pdf_sha256": by_pdf_sha, "markdown_sha256": by_md_sha}


def _raw_backing_for(item: dict[str, Any], index: dict) -> dict:
    source_id = str(item.get("source_id") or "")
    pdf_sha = str(item.get("pdf_sha256") or "")
    md_sha = str(item.get("markdown_sha256") or "")
    matches: list[dict[str, Any]] = []
    for key, value in (("source_id", source_id), ("pdf_sha256", pdf_sha), ("markdown_sha256", md_sha)):
        if not value:
            continue
        paths = index.get(key, {}).get(value, [])
        if paths:
            matches.append({"by": key, "value": value, "paths": paths})
    return {"found": bool(matches), "matches": matches}


def _formal_state_errors(folder: Path, pid: str, metadata: dict, catalog: dict) -> list[str]:
    """Formal-ingest state checks folded in from the v2.2 state-machine audit."""
    errors: list[str] = []
    ledger = PaperNumberLedger()
    markers = list(folder.glob("*.paper.number"))
    marker_number = ledger.paper_number_from_marker(folder) or ""
    if not markers:
        errors.append("missing <16-digit>.paper.number marker")
    elif not marker_number:
        errors.append(f"marker filename is not a 16-digit number: {markers[0].name}")
    # folder name == catalog.paper_id
    if catalog and catalog.get("paper_id") and catalog["paper_id"] != pid:
        errors.append(f"catalog.paper_id ({catalog['paper_id']}) != folder name ({pid})")
    # catalog.paper_number == marker
    if marker_number and catalog and catalog.get("paper_number") and catalog["paper_number"] != marker_number:
        errors.append(f"catalog.paper_number ({catalog['paper_number']}) != marker ({marker_number})")
    # metadata completeness + match
    errors.extend(validate_metadata_completeness_for_commit(metadata))
    if not metadata_is_matched(metadata):
        errors.append("metadata.metadata_match.status is not matched/manual_confirmed")
    if not metadata_doi(metadata):
        errors.append("metadata.identifiers.doi missing")
    # catalog content-only
    if catalog:
        errors.extend(validate_catalog_schema(catalog))
        for k in find_forbidden_catalog_keys(catalog):
            errors.append(f"catalog contains forbidden bibliographic key: {k}")
        # asset_refs point to existing files
        refs = catalog.get("asset_refs") or {}
        for field, val in (("markdown", "markdown"), ("pdf", "pdf"), ("metadata", "metadata"), ("catalog", "catalog")):
            ref = str(refs.get(val) or "")
            if not ref:
                errors.append(f"catalog.asset_refs.{val} missing")
            elif not (folder / ref).exists():
                errors.append(f"catalog.asset_refs.{val} does not exist: {ref}")
    # transient files must not be in the formal library
    for pattern in _TRANSIENT_PATTERNS:
        for vestige in folder.glob(pattern):
            errors.append(f"transient file in formal library: {vestige.name}")
    if (folder / "output").exists():
        errors.append("MinerU output/ dir in formal library")
    # suspicious paper_id
    if pid.isdigit() and len(pid) == 6:
        errors.append("paper_id is a 6-digit source_id (not formalized)")
    if any(hint in pid.lower() for hint in _SUSPICIOUS_PAPER_ID_HINTS) and not _PAPER_ID_STRUCTURE_RE.match(pid):
        errors.append(f"paper_id looks suspicious (doi-like/download/fulltext): {pid}")
    if not _PAPER_ID_STRUCTURE_RE.match(pid) and not (pid.isdigit()):
        errors.append(f"paper_id lacks year_author_title structure: {pid}")
    # metadata.pdf.path points at the formal library PDF
    pdf_path_field = str((metadata.get("pdf") or {}).get("path") or "")
    if pdf_path_field and pid not in pdf_path_field:
        errors.append("metadata.pdf.path does not point at the formal library PDF")
    return errors


def audit_formal_imports(
    *,
    papers_dir: Path = PAPERS_DIR,
    paper_raw_dir: Path = PAPER_RAW_DIR,
    all_catalog_path: Path = ALL_CATALOG_PATH,
    ledger_path: Path = PAPER_NUMBER_LEDGER_PATH,
    apply: bool = False,
    delete_safe: bool = False,
    quarantine: bool = False,
) -> dict:
    raw_index = _raw_backing_index(paper_raw_dir)
    items: list[dict[str, Any]] = []
    deleted: list[str] = []
    quarantined: list[dict[str, str]] = []
    if papers_dir.exists():
        for folder in sorted(p for p in papers_dir.iterdir() if p.is_dir()):
            if folder.name == "quarantine":
                continue
            pid = folder.name
            readiness = assess_paper_raw_commit_readiness(
                folder,
                file_prefix=pid,
                papers_dir=papers_dir,
                check_duplicates=False,
            )
            metadata = readiness.get("metadata") or {}
            catalog = readiness.get("catalog") or {}
            pdf_path = folder / f"{pid}.pdf"
            md_path = folder / f"{pid}.md"
            state_errors = _formal_state_errors(folder, pid, metadata, catalog) if metadata else []
            all_errors = list(readiness["errors"]) + state_errors
            item = {
                "paper_id": pid,
                "folder": str(folder),
                "status": "ok" if not all_errors else "bad",
                "errors": all_errors,
                "warnings": readiness["warnings"],
                "source_id": str(metadata.get("source_id") or catalog.get("source_id") or ""),
                "pdf_sha256": str(((metadata.get("pdf") or {}).get("sha256") or "")) or _sha(pdf_path),
                "markdown_sha256": str(((metadata.get("content") or {}).get("markdown_sha256") or "")) or _sha(md_path),
            }
            backing = _raw_backing_for(item, raw_index)
            item["raw_backing"] = backing
            item["delete_decision"] = (
                "safe_to_delete" if item["errors"] and backing["found"]
                else "unsafe_delete_requires_confirmation" if item["errors"]
                else "keep"
            )
            if apply and delete_safe and item["delete_decision"] == "safe_to_delete":
                shutil.rmtree(folder)
                item["deleted"] = True
                deleted.append(pid)
            else:
                item["deleted"] = False
            # quarantine clearly-bad entries (marker/metadata/catalog structural errors)
            item["quarantine_decision"] = "quarantine" if state_errors else "keep"
            if apply and quarantine and item["quarantine_decision"] == "quarantine":
                qdir = papers_dir.parent / "papers_quarantine" / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{pid}"
                qdir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(folder), qdir / pid)
                item["quarantined"] = True
                quarantined.append({"paper_id": pid, "quarantine_dir": str(qdir / pid)})
            else:
                item["quarantined"] = False
            items.append(item)

    rebuilt = False
    if deleted or quarantined:
        AllCatalogBuilder(papers_dir, all_catalog_path, PaperNumberLedger(ledger_path)).build(write=True)
        rebuilt = True
    return {
        "created_at": now_iso(),
        "applied": bool(apply and (delete_safe or quarantine)),
        "deleted_count": len(deleted),
        "deleted": deleted,
        "quarantined_count": len(quarantined),
        "quarantined": quarantined,
        "rebuilt_catalog_indexes": rebuilt,
        "items": items,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit formal paper_raw imports and optionally delete safe bad copies.")
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--all-catalog-path", type=Path, default=ALL_CATALOG_PATH)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--delete-safe", action="store_true")
    parser.add_argument("--quarantine", action="store_true", help="move clearly-bad formal entries to papers_quarantine/ (requires --apply)")
    args = parser.parse_args()

    report = audit_formal_imports(
        papers_dir=args.papers_dir,
        paper_raw_dir=args.paper_raw_dir,
        all_catalog_path=args.all_catalog_path,
        ledger_path=args.ledger_path,
        apply=args.apply,
        delete_safe=args.delete_safe,
        quarantine=args.quarantine,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    print(text)
    unsafe = [i for i in report["items"] if i["delete_decision"] == "unsafe_delete_requires_confirmation"]
    bad = [i for i in report["items"] if i["status"] == "bad" and not i.get("quarantined")]
    return 1 if (unsafe or bad) else 0


if __name__ == "__main__":
    raise SystemExit(main())
