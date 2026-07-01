"""Preflight local paper_raw workspaces before expensive conversion."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import PAPER_RAW_DIR, PAPERS_DIR
from src.discovery.models import normalize_doi
from src.file_fingerprint import compute_sha256
from src.naming import safe_child
from src.services.metadata_quality import is_valid_normalized_doi
from src.services.v2_library import (
    metadata_is_matched,
    now_iso,
    validate_metadata_schema,
)
from src.utils.atomic_io import atomic_write_json


_SOURCE_ID_RE = re.compile(r"^\d{6}$")
_BLOCKING_STATUSES = {
    "metadata_missing",
    "metadata_invalid",
    "doi_invalid",
    "metadata_unmatched",
    "doi_duplicate",
    "pdf_missing",
    "pdf_invalid",
    "pdf_sha_duplicate",
}


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _source_ids(root: Path, all_sources: bool, one: str | None) -> list[str]:
    if one:
        if not _SOURCE_ID_RE.match(one):
            raise ValueError(f"invalid paper_raw id: {one}")
        return [one]
    if all_sources:
        return sorted(p.name for p in root.iterdir() if p.is_dir() and _SOURCE_ID_RE.match(p.name))
    raise ValueError("--all or --paper-raw-id is required")


def _formal_sets(papers_dir: Path) -> tuple[set[str], set[str]]:
    dois: set[str] = set()
    shas: set[str] = set()
    if papers_dir.exists():
        for meta_path in papers_dir.glob("*/*.metadata.json"):
            metadata = _read_json(meta_path, {})
            doi = normalize_doi(((metadata.get("identifiers") or {}).get("doi") or ""))
            sha = str(((metadata.get("pdf") or {}).get("sha256") or "")).strip().lower()
            if doi:
                dois.add(doi)
            if sha:
                shas.add(sha)
    return dois, shas


def _paper_raw_counts(root: Path) -> tuple[dict[str, int], dict[str, int]]:
    doi_counts: dict[str, int] = {}
    sha_counts: dict[str, int] = {}
    if not root.exists():
        return doi_counts, sha_counts
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and _SOURCE_ID_RE.match(p.name)):
        source_id = folder.name
        metadata = _read_json(folder / f"{source_id}.metadata.json", {})
        doi = normalize_doi(((metadata.get("identifiers") or {}).get("doi") or ""))
        if doi:
            doi_counts[doi] = doi_counts.get(doi, 0) + 1
        pdf = folder / f"{source_id}.pdf"
        if pdf.exists():
            try:
                sha = compute_sha256(pdf)
            except OSError:
                sha = ""
            if sha:
                sha_counts[sha] = sha_counts.get(sha, 0) + 1
    return doi_counts, sha_counts


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
    ):
        if status in errors:
            return status
    return "ready_for_convert"


def preflight_one(
    root: Path,
    source_id: str,
    *,
    papers_dir: Path,
    formal_dois: set[str],
    formal_shas: set[str],
    raw_doi_counts: dict[str, int],
    raw_sha_counts: dict[str, int],
) -> dict:
    folder = safe_child(root, source_id)
    meta_path = folder / f"{source_id}.metadata.json"
    pdf_path = folder / f"{source_id}.pdf"
    errors: list[str] = []
    details: list[str] = []
    doi = ""
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
        doi = normalize_doi(((metadata.get("identifiers") or {}).get("doi") or ""))
        if not doi or not is_valid_normalized_doi(doi):
            errors.append("doi_invalid")
            details.append("metadata.identifiers.doi is missing or invalid")
        if not metadata_is_matched(metadata):
            errors.append("metadata_unmatched")
            details.append("metadata_match.status must be matched or manual_confirmed")
        if doi and doi in formal_dois:
            errors.append("doi_duplicate")
            details.append(f"DOI already exists in formal library: {doi}")
        if doi and raw_doi_counts.get(doi, 0) > 1:
            errors.append("doi_duplicate")
            details.append(f"DOI appears multiple times in paper_raw: {doi}")

    if not pdf_path.exists():
        errors.append("pdf_missing")
        details.append("PDF file missing")
    elif not _pdf_has_magic(pdf_path):
        errors.append("pdf_invalid")
        details.append("PDF magic does not start with %PDF")
    else:
        pdf_sha = compute_sha256(pdf_path)
        if pdf_sha in formal_shas:
            errors.append("pdf_sha_duplicate")
            details.append("PDF sha256 already exists in formal library")
        if raw_sha_counts.get(pdf_sha, 0) > 1:
            errors.append("pdf_sha_duplicate")
            details.append("PDF sha256 appears multiple times in paper_raw")

    errors = sorted(set(errors), key=errors.index)
    markdown_path = folder / f"{source_id}.md"
    images_dir = folder / "images"
    has_markdown = markdown_path.exists() and markdown_path.stat().st_size > 0
    has_images_dir = images_dir.exists() and images_dir.is_dir()
    if not errors and has_markdown and has_images_dir:
        status = "converted"
        details.append("paper_raw already has converted Markdown/assets")
    else:
        status = _status_from_errors(errors)
    item = {
        "source_id": source_id,
        "status": status,
        "blocking": status in _BLOCKING_STATUSES,
        "doi": doi,
        "pdf_sha256": pdf_sha,
        "has_markdown": has_markdown,
        "has_images_dir": has_images_dir,
        "errors": errors,
        "details": details,
        "created_at": now_iso(),
    }
    atomic_write_json(folder / ".import_status.json", item, indent=2)
    return item


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight v2 paper_raw import workspaces.")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--paper-raw-id", default=None)
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    source_ids = _source_ids(args.paper_raw_dir, args.all, args.paper_raw_id)
    formal_dois, formal_shas = _formal_sets(args.papers_dir)
    raw_doi_counts, raw_sha_counts = _paper_raw_counts(args.paper_raw_dir)
    items = [
        preflight_one(
            args.paper_raw_dir,
            source_id,
            papers_dir=args.papers_dir,
            formal_dois=formal_dois,
            formal_shas=formal_shas,
            raw_doi_counts=raw_doi_counts,
            raw_sha_counts=raw_sha_counts,
        )
        for source_id in source_ids
    ]
    result = {"items": items, "blocking_count": sum(1 for item in items if item["blocking"])}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if args.strict and result["blocking_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
