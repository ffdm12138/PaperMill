"""Preflight local paper_raw workspaces before expensive conversion."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import PAPER_RAW_DIR, PAPERS_DIR
from src.utils.identifiers import normalize_doi
from src.file_fingerprint import compute_file_hashes
from src.naming import safe_child
from src.metadata.freeze import assert_metadata_frozen
from src.services.ingest_duplicate_guard import check_doi_duplicate, check_pdf_duplicate
from src.utils.identifiers import PAPER_NUMBER_RE, validate_paper_raw_id
from src.services.ingest_state import write_import_status
from src.services.metadata_quality import is_valid_normalized_doi
from src.services.source_records import validate_metadata_source_record_exists
from src.ingest.models import now_iso
from src.metadata.schema import validate_metadata_schema
from src.utils.atomic_io import atomic_write_json
_BLOCKING_STATUSES = {
    "metadata_missing",
    "metadata_invalid",
    "doi_invalid",
    "metadata_unmatched",
    "doi_duplicate",
    "pdf_missing",
    "pdf_invalid",
    "pdf_sha_duplicate",
    "pdf_md5_duplicate",
    "pdf_md5_collision_or_inconsistent_hash",
}
FORMALIZE_METADATA_LAYERED_HINT = "conversion may proceed, but formalize/commit is blocked until Metadata match/freeze succeeds"


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _source_ids(root: Path, all_sources: bool, one: str | None) -> list[str]:
    if one:
        return [validate_paper_raw_id(one)]
    if all_sources:
        return sorted(p.name for p in root.iterdir() if p.is_dir() and PAPER_NUMBER_RE.match(p.name))
        # numbered-workspace enumeration only; this preflight targets staging
        # workspaces. Legacy/untitled dedup is handled by ingest_duplicate_guard.
    raise ValueError("--all or --paper-number is required")


def _pdf_has_magic(path: Path) -> bool:
    try:
        return path.read_bytes()[:5].startswith(b"%PDF")
    except OSError:
        return False


def _status_from_errors(errors: list[str]) -> str:
    for status in (
        "metadata_missing",
        "metadata_invalid",
        "pdf_missing",
        "pdf_invalid",
        "doi_invalid",
        "metadata_unmatched",
        "doi_duplicate",
        "pdf_sha_duplicate",
        "pdf_md5_duplicate",
        "pdf_md5_collision_or_inconsistent_hash",
    ):
        if status in errors:
            return status
    return "ready_for_convert"


def preflight_one(
    root: Path,
    source_id: str,
    *,
    papers_dir: Path,
) -> dict:
    folder = safe_child(root, source_id)
    meta_path = folder / f"{source_id}.metadata.json"
    pdf_path = folder / f"{source_id}.pdf"
    errors: list[str] = []
    details: list[str] = []
    doi = ""
    pdf_md5 = ""
    pdf_sha = ""

    if not meta_path.exists():
        errors.append("metadata_missing")
        details.append("metadata file missing")
        metadata = {}
    else:
        metadata = _read_json(meta_path, {})
        schema_errors = validate_metadata_schema(metadata)
        if schema_errors:
            errors.append("metadata_invalid")
            details.extend(schema_errors)
        # Check that source.raw_record_path points to an existing file
        raw_rp = (metadata.get("source") or {}).get("raw_record_path", "")
        for src_err in validate_metadata_source_record_exists(folder, raw_rp):
            errors.append("source_record_missing")
            details.append(src_err)
        doi = normalize_doi(((metadata.get("identifiers") or {}).get("doi") or ""))
        if not doi or not is_valid_normalized_doi(doi):
            errors.append("doi_invalid")
            details.append("metadata.identifiers.doi is missing or invalid")
        try:
            assert_metadata_frozen(folder, source_id)
        except Exception as exc:
            errors.append("metadata_unmatched")
            details.append(f"independent metadata match/freeze receipt invalid: {exc}")
        if doi:
            dup_doi = check_doi_duplicate(
                doi,
                paper_raw_dir=root,
                papers_dir=papers_dir,
                skip_paper_number=source_id,
            )
            if dup_doi.blocking:
                errors.append("doi_duplicate")
                for ref in dup_doi.refs:
                    details.append(f"DOI duplicate in {ref.scope}/{ref.paper_number or ref.paper_name}: {doi}")

    if not pdf_path.exists():
        errors.append("pdf_missing")
        details.append("PDF file missing")
    elif not _pdf_has_magic(pdf_path):
        errors.append("pdf_invalid")
        details.append("PDF magic does not start with %PDF")
    else:
        hashes = compute_file_hashes(pdf_path)
        pdf_md5 = hashes["md5"]
        pdf_sha = hashes["sha256"]
        dup_pdf = check_pdf_duplicate(
            pdf_path,
            paper_raw_dir=root,
            papers_dir=papers_dir,
            skip_paper_number=source_id,
        )
        if dup_pdf.blocking:
            if "pdf_sha256_duplicate" in dup_pdf.reasons:
                errors.append("pdf_sha_duplicate")
            if "pdf_md5_duplicate" in dup_pdf.reasons:
                errors.append("pdf_md5_duplicate")
            if "pdf_md5_collision_or_inconsistent_hash" in dup_pdf.reasons:
                errors.append("pdf_md5_collision_or_inconsistent_hash")
            for ref in dup_pdf.refs:
                details.append(f"PDF duplicate in {ref.scope}/{ref.paper_number or ref.paper_name}")

    errors = sorted(set(errors), key=errors.index)
    # Layered-semantics hint: conversion is allowed without metadata, but
    # formalize/commit is not. Surface this once on metadata-gate failures so
    # operators do not misread "doi_invalid / metadata_unmatched" as a
    # conversion blocker.
    if "doi_invalid" in errors or "metadata_unmatched" in errors:
        details.append(FORMALIZE_METADATA_LAYERED_HINT)
    markdown_path = folder / f"{source_id}.md"
    images_dir = folder / "images"
    has_markdown = markdown_path.exists() and markdown_path.stat().st_size > 0
    has_images_dir = images_dir.exists() and images_dir.is_dir()
    if has_markdown and has_images_dir:
        status = "converted"
        details.append("paper_raw already has converted Markdown/assets; formal metadata gates remain separate")
    else:
        status = _status_from_errors(errors)
    item = {
        "paper_number": source_id,
        "paper_raw_id": source_id,
        "status": status,
        "blocking": status in _BLOCKING_STATUSES,
        "doi": doi,
        "pdf_md5": pdf_md5,
        "pdf_sha256": pdf_sha,
        "has_markdown": has_markdown,
        "has_images_dir": has_images_dir,
        "errors": errors,
        "details": details,
        "created_at": now_iso(),
    }
    write_import_status(
        folder,
        status,
        errors=errors,
        extra={
            "paper_number": source_id,
            "paper_raw_id": source_id,
            "blocking": status in _BLOCKING_STATUSES,
            "doi": doi,
            "pdf_md5": pdf_md5,
            "pdf_sha256": pdf_sha,
            "has_markdown": has_markdown,
            "has_images_dir": has_images_dir,
            "details": details,
        },
    )
    return item


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight v2 paper_raw import workspaces.")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--paper-number", default=None)
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    source_ids = _source_ids(args.paper_raw_dir, args.all, args.paper_number)
    items = [
        preflight_one(
            args.paper_raw_dir,
            source_id,
            papers_dir=args.papers_dir,
        )
        for source_id in source_ids
    ]
    result = {"items": items, "blocking_count": sum(1 for item in items if item["blocking"])}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if args.strict and result["blocking_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
