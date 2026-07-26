"""Metadata v2.0 citation-record schema and deterministic factory."""
from __future__ import annotations

import re
from typing import Any

from src.metadata.source_records import (
    SourceRecordPathEscapeError,
    validate_metadata_source_rel_path,
)


METADATA_SCHEMA_VERSION = "2.0"
PAPER_NUMBER_RE = re.compile(r"^[0-9]{16}$")
FORBIDDEN_METADATA_TOP_LEVEL_KEYS = {
    "abstract", "keywords", "pdf", "content", "notes", "bibtex",
    "citation_key", "paper_name", "paper_id", "metadata_match",
}
FORBIDDEN_METADATA_TITLE_KEYS = {"short_zh", "translated_zh", "content_title_zh"}
FORBIDDEN_METADATA_SOURCE_KEYS = {"raw_record", "providers"}


def empty_metadata(paper_number: str, source_type: str = "manual_pdf") -> dict:
    number = str(paper_number)
    valid_number = number if PAPER_NUMBER_RE.fullmatch(number) else ""
    return {
        "schema_version": METADATA_SCHEMA_VERSION,
        "paper_number": valid_number,
        "paper_raw_id": valid_number,
        "source_type": source_type,
        "entry_type": "article",
        "title": {"original": "", "subtitle": ""},
        "authors": [{"full_name": "", "family": "", "given": "", "orcid": "", "affiliation": ""}],
        "first_author": {"family": "", "display": ""},
        "year": None,
        "date": {"published": "", "online": "", "accessed": ""},
        "container": {"journal": "", "booktitle": "", "conference": "", "series": "", "publisher": "", "institution": "", "school": ""},
        "publication": {"volume": "", "number": "", "issue": "", "pages": "", "article_number": "", "edition": ""},
        "identifiers": {"doi": "", "arxiv_id": "", "isbn": "", "issn": "", "pmid": "", "pmcid": "", "openalex_id": "", "crossref_id": ""},
        "links": {"url": "", "pdf_url": "", "publisher_url": "", "repository_url": ""},
        "language": "en",
        "source": {"kind": source_type, "provider": "", "query": "", "retrieved_at": "", "raw_record_path": ""},
    }


def validate_metadata_schema(data: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["metadata must be an object"]
    if str(data.get("schema_version") or "") != METADATA_SCHEMA_VERSION:
        errors.append(f"metadata.schema_version must be {METADATA_SCHEMA_VERSION}")
    for key in sorted(FORBIDDEN_METADATA_TOP_LEVEL_KEYS):
        if key in data:
            errors.append(f"metadata.{key} is forbidden in schema {METADATA_SCHEMA_VERSION}")
    title = data.get("title") if isinstance(data.get("title"), dict) else {}
    for key in sorted(FORBIDDEN_METADATA_TITLE_KEYS):
        if key in title:
            errors.append(f"metadata.title.{key} is forbidden")
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    for key in sorted(FORBIDDEN_METADATA_SOURCE_KEYS):
        if key in source:
            errors.append(f"metadata.source.{key} is forbidden")
    # raw_record_path strict validation
    raw_path = str(source.get("raw_record_path") or "").strip()
    if raw_path:
        try:
            validate_metadata_source_rel_path(raw_path)
        except SourceRecordPathEscapeError as exc:
            errors.append(f"metadata.source.raw_record_path invalid: {exc}")
    required = ("schema_version", "paper_number", "paper_raw_id", "source_type", "entry_type", "title", "authors", "first_author", "year", "date", "container", "publication", "identifiers", "links", "language", "source")
    for key in required:
        if key not in data:
            errors.append(f"metadata missing {key}")
    for key in ("paper_number", "paper_raw_id"):
        if not PAPER_NUMBER_RE.fullmatch(str(data.get(key) or "")):
            errors.append(f"metadata.{key} must be 16 digits")
    nested = {
        "title": ("original", "subtitle"), "first_author": ("family", "display"),
        "date": ("published", "online", "accessed"),
        "container": ("journal", "booktitle", "conference", "series", "publisher", "institution", "school"),
        "publication": ("volume", "number", "issue", "pages", "article_number", "edition"),
        "identifiers": ("doi", "arxiv_id", "isbn", "issn", "pmid", "pmcid", "openalex_id", "crossref_id"),
        "links": ("url", "pdf_url", "publisher_url", "repository_url"),
        "source": ("kind", "provider", "query", "retrieved_at", "raw_record_path"),
    }
    for parent, keys in nested.items():
        value = data.get(parent)
        if not isinstance(value, dict):
            errors.append(f"metadata.{parent} must be an object")
            continue
        for key in keys:
            if key not in value:
                errors.append(f"metadata.{parent} missing {key}")
    authors = data.get("authors")
    if not isinstance(authors, list):
        errors.append("metadata.authors must be a list")
    else:
        for index, author in enumerate(authors):
            if not isinstance(author, dict):
                errors.append(f"metadata.authors[{index}] must be an object")
                continue
            for key in ("full_name", "family", "given", "orcid", "affiliation"):
                if key not in author:
                    errors.append(f"metadata.authors[{index}] missing {key}")
    return errors


def metadata_doi(metadata: dict) -> str:
    return str(((metadata.get("identifiers") or {}).get("doi") or "")).strip()


def first_author_family(metadata: dict) -> str:
    first = metadata.get("first_author") if isinstance(metadata.get("first_author"), dict) else {}
    family = str(first.get("family") or "").strip()
    if family:
        return family
    authors = metadata.get("authors") if isinstance(metadata.get("authors"), list) else []
    if authors and isinstance(authors[0], dict):
        return str(authors[0].get("family") or authors[0].get("full_name") or "").strip()
    return ""
