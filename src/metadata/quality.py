"""Metadata quality audit helpers for the formal v2 library."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from src.utils.jsonio import read_json_strict
from src.utils.identifiers import normalize_doi


DOI_RE = re.compile(r"^10\.\S+/\S+$")
OK_MATCH_STATUSES = {"matched", "manual_confirmed"}


def normalized_metadata_doi(metadata: dict) -> str:
    return normalize_doi(((metadata.get("identifiers") or {}).get("doi") or ""))


def is_valid_normalized_doi(doi: str) -> bool:
    return bool(DOI_RE.match(str(doi or "").strip()))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _has_named_author(metadata: dict) -> bool:
    authors = metadata.get("authors") or []
    if not isinstance(authors, list) or not authors:
        return False
    for author in authors:
        if isinstance(author, dict):
            if _text(author.get("full_name")) or _text(author.get("family")):
                return True
        elif _text(author):
            return True
    return False


def _has_first_author_family(metadata: dict) -> bool:
    first = metadata.get("first_author") or {}
    return _text(first.get("family")) != ""


def _has_year(metadata: dict) -> bool:
    try:
        int(metadata.get("year"))
    except (TypeError, ValueError):
        return False
    return True


def bibliographic_identity_gate(
    metadata: dict,
    blocking_reasons: list[str] | None = None,
) -> tuple[bool, list[str]]:
    """Return whether metadata is complete enough to be marked matched."""
    from src.metadata.schema import validate_metadata_schema

    reasons: list[str] = []
    doi = normalized_metadata_doi(metadata)
    if not doi:
        reasons.append("missing doi")
    elif not is_valid_normalized_doi(doi):
        reasons.append("invalid doi")
    if not _text((metadata.get("title") or {}).get("original")):
        reasons.append("missing title.original")
    if not _has_year(metadata):
        reasons.append("missing year")
    if not _has_named_author(metadata):
        reasons.append("missing authors")
    if not _has_first_author_family(metadata):
        reasons.append("missing first_author.family")
    if not _has_venue(metadata):
        reasons.append("missing container journal/conference/booktitle")
    for error in validate_metadata_schema(metadata):
        reasons.append(f"schema invalid: {error}")
    for reason in blocking_reasons or []:
        if _text(reason):
            reasons.append(str(reason))
    reasons = list(dict.fromkeys(reasons))
    return not reasons, reasons


def _has_venue(metadata: dict) -> bool:
    container = metadata.get("container") or {}
    return any(
        _text(container.get(key))
        for key in ("journal", "conference", "booktitle", "book_title", "venue")
    )


def metadata_quality_hard_errors(metadata: dict, match_receipt: dict | None = None) -> list[str]:
    """Return formal-library hard metadata errors."""
    errors: list[str] = []
    doi = normalized_metadata_doi(metadata)
    if not _text((metadata.get("title") or {}).get("original")):
        errors.append("missing metadata.title.original")
    if not _has_named_author(metadata):
        errors.append("missing metadata.authors")
    if not _has_year(metadata):
        errors.append("missing metadata.year")
    if not doi:
        errors.append("missing metadata.identifiers.doi")
    elif not is_valid_normalized_doi(doi):
        errors.append("invalid metadata.identifiers.doi")
    if not _has_venue(metadata):
        errors.append("missing metadata.container venue")
    status = str((match_receipt or {}).get("match_status") or "").strip()
    if status not in OK_MATCH_STATUSES:
        errors.append("independent metadata match receipt must be matched or manual_confirmed")
    return errors


def metadata_quality_warnings(metadata: dict) -> list[str]:
    """Return soft metadata quality warnings that do not block validation."""
    warnings: list[str] = []
    publication = metadata.get("publication") or {}
    container = metadata.get("container") or {}
    links = metadata.get("links") or {}
    source = metadata.get("source") or {}

    if not _text(publication.get("volume")):
        warnings.append("missing publication.volume")
    if not (_text(publication.get("issue")) or _text(publication.get("number"))):
        warnings.append("missing publication.issue or publication.number")
    if not (_text(publication.get("pages")) or _text(publication.get("article_number"))):
        warnings.append("missing publication.pages or publication.article_number")
    if not _text(container.get("publisher")):
        warnings.append("missing container.publisher")
    if not _text(links.get("url")):
        warnings.append("missing links.url")
    if not _text(source.get("raw_record_path")):
        warnings.append("missing source.raw_record_path")
    return warnings


def _read_json(path: Path) -> dict:
    return read_json_strict(path)


def _paper_number_from_folder(folder: Path) -> str:
    markers = sorted(folder.glob("*.paper.number"))
    if not markers:
        return ""
    marker = markers[0]
    try:
        data = _read_json(marker)
    except Exception:
        data = {}
    return _text(data.get("paper_number")) or marker.name.removesuffix(".paper.number")


def audit_metadata_library(papers_dir: str | Path) -> dict:
    """Audit formal paper metadata files and return a stable JSON report."""
    root = Path(papers_dir)
    papers: list[dict] = []
    doi_to_papers: dict[str, list[str]] = {}

    if root.exists():
        for folder in sorted(p for p in root.iterdir() if p.is_dir()):
            paper_name = folder.name
            metadata_path = folder / f"{paper_name}.metadata.json"
            if not metadata_path.exists():
                item = {
                    "paper_number": _paper_number_from_folder(folder),
                    "paper_name": paper_name,
                    "doi": "",
                    "hard_status": "error",
                    "errors": [f"missing metadata file: {metadata_path.name}"],
                    "warnings": [],
                }
                papers.append(item)
                continue
            metadata = _read_json(metadata_path)
            receipt_path = folder / f"{paper_name}.metadata_match.json"
            match_receipt = _read_json(receipt_path) if receipt_path.is_file() else None
            doi = normalized_metadata_doi(metadata)
            item_errors = metadata_quality_hard_errors(metadata, match_receipt)
            item = {
                "paper_number": _paper_number_from_folder(folder),
                "paper_name": paper_name,
                "doi": doi,
                "hard_status": "ok",
                "errors": item_errors,
                "warnings": metadata_quality_warnings(metadata),
            }
            papers.append(item)
            if doi and is_valid_normalized_doi(doi):
                doi_to_papers.setdefault(doi, []).append(paper_name)

    for doi, paper_names in sorted(doi_to_papers.items()):
        if len(paper_names) < 2:
            continue
        message = f"duplicate metadata.identifiers.doi: {doi}"
        duplicate_set = set(paper_names)
        for item in papers:
            if item["paper_name"] in duplicate_set and message not in item["errors"]:
                item["errors"].append(message)

    for item in papers:
        item["errors"] = sorted(item["errors"])
        item["warnings"] = sorted(item["warnings"])
        item["hard_status"] = "error" if item["errors"] else "ok"

    errors = [
        f"{item['paper_name']}: {error}"
        for item in papers
        for error in item["errors"]
    ]
    warnings = [
        f"{item['paper_name']}: {warning}"
        for item in papers
        for warning in item["warnings"]
    ]
    return {
        "total": len(papers),
        "errors": sorted(errors),
        "warnings": sorted(warnings),
        "papers": papers,
    }
