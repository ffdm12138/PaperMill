"""Deterministic citation readiness checks; never reads catalog data."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Mapping

from src.services.metadata_quality import is_valid_normalized_doi, normalized_metadata_doi
from src.metadata.citation import bibtex_from_metadata, format_reference_from_metadata


@dataclass(frozen=True)
class CitationReadinessResult:
    ready: bool
    entry_type: str
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    generated_csl: Mapping[str, object] | None
    generated_bibtex: str | None


def _text(value: object) -> str:
    return str(value or "").strip()


def citation_key_from_metadata(metadata: Mapping[str, object]) -> str:
    """Return a deterministic key whose inputs are exclusively Metadata."""
    result = validate_citation_ready(metadata)
    if not result.ready:
        raise ValueError("metadata is not citation-ready: " + "; ".join(result.errors))
    authors = metadata.get("authors") if isinstance(metadata.get("authors"), list) else []
    first = authors[0] if authors and isinstance(authors[0], dict) else {}
    family = re.sub(r"[^0-9A-Za-z]+", "", _text(first.get("family") or first.get("full_name"))) or "Anon"
    year = _text(metadata.get("year"))
    title = _text((metadata.get("title") or {}).get("original") if isinstance(metadata.get("title"), dict) else "")
    title_token = re.sub(r"[^0-9A-Za-z]+", "", title.split()[0] if title.split() else "") or "Work"
    identifiers = metadata.get("identifiers") if isinstance(metadata.get("identifiers"), dict) else {}
    links = metadata.get("links") if isinstance(metadata.get("links"), dict) else {}
    stable = normalized_metadata_doi(dict(metadata)) or next((_text(value) for value in identifiers.values() if _text(value)), "") or _text(links.get("url")) or title
    digest = hashlib.sha256(stable.casefold().encode("utf-8")).hexdigest()[:8]
    return f"{family}{year}{title_token}{digest}"


def metadata_to_csl(metadata: Mapping[str, object]) -> dict:
    """Create a small, stable CSL-JSON record from metadata v2.0 only."""
    authors = metadata.get("authors") if isinstance(metadata.get("authors"), list) else []
    csl_authors = []
    for author in authors:
        if isinstance(author, dict):
            csl_authors.append({"family": _text(author.get("family")), "given": _text(author.get("given"))})
    container = metadata.get("container") if isinstance(metadata.get("container"), dict) else {}
    publication = metadata.get("publication") if isinstance(metadata.get("publication"), dict) else {}
    identifiers = metadata.get("identifiers") if isinstance(metadata.get("identifiers"), dict) else {}
    entry_type = _text(metadata.get("entry_type")) or "article"
    csl_type = {
        "article": "article-journal", "inproceedings": "paper-conference",
        "paper-conference": "paper-conference", "incollection": "chapter",
        "chapter": "chapter", "phdthesis": "thesis", "mastersthesis": "thesis",
        "techreport": "report", "preprint": "article", "book": "book",
    }.get(entry_type, entry_type)
    return {
        "type": csl_type, "title": _text((metadata.get("title") or {}).get("original") if isinstance(metadata.get("title"), dict) else ""),
        "author": csl_authors, "issued": {"date-parts": [[metadata.get("year")]]},
        "container-title": _text(container.get("journal") or container.get("conference") or container.get("booktitle")),
        "publisher": _text(container.get("publisher") or container.get("institution")),
        "DOI": _text(identifiers.get("doi")), "URL": _text((metadata.get("links") or {}).get("url") if isinstance(metadata.get("links"), dict) else ""),
        "volume": _text(publication.get("volume")), "issue": _text(publication.get("issue") or publication.get("number")),
        "page": _text(publication.get("pages") or publication.get("article_number")),
    }


def validate_citation_ready(metadata: Mapping[str, object]) -> CitationReadinessResult:
    entry_type = _text(metadata.get("entry_type")) or "article"
    errors: list[str] = []
    title = _text((metadata.get("title") or {}).get("original") if isinstance(metadata.get("title"), dict) else "")
    authors = metadata.get("authors") if isinstance(metadata.get("authors"), list) else []
    try:
        year = int(metadata.get("year"))
    except (TypeError, ValueError):
        year = 0
    if not title: errors.append("metadata.title.original is required")
    normalized_authors = [_text(a.get("family") or a.get("full_name")) for a in authors if isinstance(a, dict)]
    if not normalized_authors or any(not value for value in normalized_authors): errors.append("ordered authors are required and may not contain empty authors")
    if not 1500 <= year <= 3000: errors.append("valid publication year is required")
    doi = normalized_metadata_doi(dict(metadata))
    container = metadata.get("container") if isinstance(metadata.get("container"), dict) else {}
    links = metadata.get("links") if isinstance(metadata.get("links"), dict) else {}
    stable = bool(doi and is_valid_normalized_doi(doi)) or bool(_text(links.get("url"))) or bool(_text((metadata.get("identifiers") or {}).get("isbn")) if isinstance(metadata.get("identifiers"), dict) else "")
    if entry_type == "article":
        if not _text(container.get("journal")): errors.append("journal article requires container.journal")
        if not (doi and is_valid_normalized_doi(doi)): errors.append("journal article requires valid DOI")
    elif entry_type in {"inproceedings", "paper-conference"}:
        if not _text(container.get("conference") or container.get("booktitle")): errors.append("conference paper requires conference/container")
        if not stable: errors.append("conference paper requires stable identifier or URL")
    elif entry_type in {"incollection", "chapter"}:
        if not _text(container.get("booktitle")): errors.append("book chapter requires book title")
        if not _text(container.get("publisher")): errors.append("book chapter requires publisher")
        if not stable: errors.append("book chapter requires DOI, ISBN, or stable identifier")
    elif entry_type in {"phdthesis", "mastersthesis", "thesis", "techreport", "report"}:
        if not _text(container.get("institution") or container.get("school") or container.get("publisher")): errors.append(f"{entry_type} requires institution/publisher")
        if not stable: errors.append(f"{entry_type} requires stable identifier or URL")
    elif entry_type in {"book", "preprint", "misc"}:
        if not stable: errors.append(f"{entry_type} requires stable identifier or URL")
    else: errors.append(f"unsupported entry_type: {entry_type}")
    if errors: return CitationReadinessResult(False, entry_type, tuple(errors), (), None, None)
    csl = metadata_to_csl(metadata)
    bib = bibtex_from_metadata(dict(metadata))
    try: format_reference_from_metadata(dict(metadata), style="apa")
    except Exception as exc: return CitationReadinessResult(False, entry_type, (f"APA render failed: {exc}",), (), csl, bib)
    return CitationReadinessResult(True, entry_type, (), (), csl, bib)
