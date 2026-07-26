"""Metadata enrichment service — DOI extraction, Crossref query, metadata normalization.

Provides:
- DOI extraction from filenames, fetch metadata record, MinerU markdown, PDF text (pymupdf)
- Crossref API metadata query by DOI
- Normalized bibliographic metadata from Crossref/OpenAlex/Semantic Scholar/Unpaywall
- Fetch metadata record enrichment

All network calls are isolated so tests can mock them.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from src.utils.identifiers import extract_doi_from_text, normalize_doi

# Restored: heading matcher lost in the Stage-3 DOI-regex dedup; the PDF
# DOI scan must stop at the references section (optional markdown marks).
_REFERENCES_HEADING_RE = re.compile(
    r"^\s{0,6}(?:#{1,6}\s*)?(references|bibliography|参考文献)",
    re.IGNORECASE | re.MULTILINE,
)
from src.utils.naming import sanitize_paper_name, validate_paper_name

def extract_doi_from_filename(filename: str) -> str | None:
    """Try to extract a DOI from a PDF filename stem."""
    return extract_doi_from_text(Path(filename).stem)


def extract_doi_from_metadata_record(record: dict) -> str | None:
    """Extract DOI from a fetch/search metadata record."""
    for key in ("doi", "DOI", "Doi"):
        val = record.get(key, "")
        if val:
            doi = extract_doi_from_text(str(val))
            if doi:
                return doi
    return None


def extract_doi_from_markdown(md_text: str, max_lines: int = 60) -> str | None:
    """Extract DOI from the first *max_lines* of MinerU markdown."""
    if not md_text:
        return None
    lines = md_text.split("\n")[:max_lines]
    return extract_doi_from_text("\n".join(lines))


# ── PyMuPDF optional import ────────────────────────────────────────────

def _has_pymupdf() -> bool:
    try:
        import fitz  # noqa: F401
        return True
    except ImportError:
        return False


def extract_doi_from_pdf_file(pdf_path: str | Path) -> str | None:
    """Extract DOI from a PDF file using pymupdf (first 3 pages).

    Returns None if pymupdf is not installed (warning logged, not fatal).
    """
    if not _has_pymupdf():
        logger.warning("PyMuPDF not installed; skipped PDF text DOI extraction")
        return None
    import fitz

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        return None
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:
        logger.warning(f"pymupdf failed to open {pdf_path.name}: {exc}")
        return None
    try:
        for page_num in range(min(3, len(doc))):
            page = doc[page_num]
            text = page.get_text()
            ref_match = _REFERENCES_HEADING_RE.search(text)
            if ref_match:
                text = text[:ref_match.start()]
            doi = extract_doi_from_text(text)
            if doi:
                return doi
            if ref_match:
                break
    finally:
        doc.close()
    return None


# ── Metadata normalization ─────────────────────────────────────────────

@dataclass
class EnrichmentResult:
    """Unified enrichment output."""
    doi: str = ""
    title: str = ""
    year: int | None = None
    authors: list[str] = field(default_factory=list)
    first_author: str = ""
    venue: str = ""
    publisher: str = ""
    volume: str = ""
    number: str = ""
    issue: str = ""
    pages: str = ""
    article_number: str = ""
    url: str = ""
    issn: str = ""
    published: str = ""
    source: str = ""           # crossref / openalex / semantic_scholar / unpaywall / manual
    confidence: float = 0.0
    chinese_title: str = ""
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "doi": self.doi,
            "title": self.title,
            "year": self.year,
            "authors": self.authors,
            "first_author": self.first_author,
            "venue": self.venue,
            "publisher": self.publisher,
            "volume": self.volume,
            "number": self.number,
            "issue": self.issue,
            "pages": self.pages,
            "article_number": self.article_number,
            "url": self.url,
            "issn": self.issn,
            "published": self.published,
            "source": self.source,
            "confidence": self.confidence,
            "chinese_title": self.chinese_title,
            "warnings": self.warnings,
            "raw": self.raw,
        }


def normalize_crossref_metadata(raw: dict) -> dict:
    """Normalize Crossref API work response into canonical metadata fields."""
    def _first(value: Any) -> str:
        if isinstance(value, list):
            return str(value[0]) if value else ""
        return str(value) if value not in (None, "") else ""

    def _date_string(value: Any) -> str:
        parts = (value or {}).get("date-parts") or []
        if not parts or not parts[0]:
            return ""
        return "-".join(str(part).zfill(2) if i else str(part) for i, part in enumerate(parts[0]))

    title = ""
    title_list = raw.get("title") or []
    if isinstance(title_list, list) and title_list:
        title = str(title_list[0])
    elif isinstance(title_list, str):
        title = title_list

    year = None
    for date_field in ("published-print", "published-online", "issued", "created"):
        date_parts = (raw.get(date_field) or {}).get("date-parts") or []
        if date_parts and date_parts[0]:
            try:
                year = int(date_parts[0][0])
                break
            except (TypeError, ValueError, IndexError):
                continue

    authors = []
    for a in raw.get("author") or []:
        family = a.get("family", "")
        given = a.get("given", "")
        if family:
            authors.append(family)
        elif given:
            authors.append(given)
    first_author = authors[0] if authors else ""

    venue = ""
    container = raw.get("container-title") or []
    if isinstance(container, list) and container:
        venue = str(container[0])
    elif isinstance(container, str):
        venue = container

    publisher = raw.get("publisher", "")
    doi = normalize_doi(raw.get("DOI") or raw.get("doi") or "")
    issue = str(raw.get("issue") or "").strip()
    number = str(raw.get("number") or issue or "").strip()
    published = ""
    for date_field in ("published-print", "published-online", "issued", "created"):
        published = _date_string(raw.get(date_field))
        if published:
            break

    return {
        "doi": doi,
        "title": title,
        "year": year,
        "authors": authors,
        "first_author": first_author,
        "venue": venue,
        "journal": venue,
        "publisher": publisher,
        "volume": str(raw.get("volume") or "").strip(),
        "number": number,
        "issue": issue,
        "pages": str(raw.get("page") or raw.get("pages") or "").strip(),
        "article_number": str(raw.get("article-number") or raw.get("article_number") or "").strip(),
        "url": str(raw.get("URL") or raw.get("url") or "").strip(),
        "issn": _first(raw.get("ISSN") or raw.get("issn")),
        "published": published,
        "source": "crossref",
    }


def normalize_openalex_metadata(raw: dict) -> dict:
    """Normalize OpenAlex work object into canonical metadata fields."""
    title = raw.get("title", "") or ""
    year = raw.get("publication_year")
    if year is not None:
        try:
            year = int(year)
        except (TypeError, ValueError):
            year = None

    authors = []
    for a in raw.get("authorships") or []:
        author_info = a.get("author") or {}
        name = author_info.get("display_name", "")
        if name:
            authors.append(name)
    first_author = authors[0] if authors else ""

    venue = ""
    primary = raw.get("primary_location") or {}
    source = primary.get("source") or {}
    venue = source.get("display_name", "")

    return {
        "title": title,
        "year": year,
        "authors": authors,
        "first_author": first_author,
        "venue": venue,
        "publisher": "",
        "source": "openalex",
    }


def normalize_semantic_scholar_metadata(raw: dict) -> dict:
    """Normalize Semantic Scholar paper object into canonical metadata fields."""
    title = raw.get("title", "") or ""
    year = raw.get("year")
    if year is not None:
        try:
            year = int(year)
        except (TypeError, ValueError):
            year = None

    authors = []
    for a in raw.get("authors") or []:
        name = a.get("name", "")
        if name:
            authors.append(name)
    first_author = authors[0] if authors else ""

    venue = raw.get("venue", "") or raw.get("journal", "") or ""

    return {
        "title": title,
        "year": year,
        "authors": authors,
        "first_author": first_author,
        "venue": venue,
        "publisher": "",
        "source": "semantic_scholar",
    }


def normalize_unpaywall_metadata(raw: dict) -> dict:
    """Normalize Unpaywall result into canonical metadata fields."""
    title = raw.get("title", "") or ""
    year = raw.get("year")
    if year is not None:
        try:
            year = int(year)
        except (TypeError, ValueError):
            year = None

    authors = []
    for a in raw.get("z_authors") or []:
        family = a.get("family", "")
        given = a.get("given", "")
        name = f"{family}, {given}" if family and given else (family or given)
        if name:
            authors.append(name)
    first_author = authors[0] if authors else ""

    venue = raw.get("journal_name", "") or ""

    return {
        "title": title,
        "year": year,
        "authors": authors,
        "first_author": first_author,
        "venue": venue,
        "publisher": raw.get("publisher", ""),
        "source": "unpaywall",
    }


def normalize_bibliographic_metadata(raw: dict, source: str = "") -> dict:
    """Normalize metadata from any known source into canonical fields.

    source: one of "crossref", "openalex", "semantic_scholar", "unpaywall", or "" for auto-detect.
    """
    if not raw:
        return {"title": "", "year": None, "authors": [], "first_author": "",
                "venue": "", "publisher": "", "source": source or "unknown"}

    # Auto-detect source
    if not source:
        if "author" in raw and "publisher" in raw:
            source = "crossref"
        elif "authorships" in raw:
            source = "openalex"
        elif "paperId" in raw or "externalIds" in raw:
            source = "semantic_scholar"
        elif "z_authors" in raw:
            source = "unpaywall"

    if source == "crossref":
        result = normalize_crossref_metadata(raw)
    elif source == "openalex":
        result = normalize_openalex_metadata(raw)
    elif source == "semantic_scholar":
        result = normalize_semantic_scholar_metadata(raw)
    elif source == "unpaywall":
        result = normalize_unpaywall_metadata(raw)
    else:
        # Generic fallback: try common fields
        title = raw.get("title", "") or raw.get("name", "") or ""
        year = raw.get("year") or raw.get("publication_year")
        if year is not None:
            try:
                year = int(year)
            except (TypeError, ValueError):
                year = None
        authors = raw.get("authors") or raw.get("author") or []
        if isinstance(authors, str):
            authors = [authors]
        first_author = authors[0] if authors else ""
        result = {
            "title": str(title),
            "year": year,
            "authors": list(authors),
            "first_author": str(first_author),
            "venue": str(raw.get("venue") or raw.get("journal") or ""),
            "publisher": str(raw.get("publisher") or ""),
            "source": source or "generic",
        }

    return result


# ── Crossref API query ─────────────────────────────────────────────────

def query_crossref_by_doi(doi: str, timeout: int = 15) -> dict | None:
    """Query Crossref API for work metadata by DOI.

    Returns the raw 'message' dict from Crossref response, or None on failure.
    This function IS the network-callable unit — tests must mock it.

    HTTP goes through the unified ``ProviderClient`` (shared limiter,
    retry/backoff, circuit breaker, proxy-configured transport) via
    :func:`src.discovery.resolve_crossref.get_crossref_work_by_doi`.
    """
    from src.discovery.resolve_crossref import get_crossref_work_by_doi

    normalized = normalize_doi(doi)
    if not normalized:
        return None
    message = get_crossref_work_by_doi(normalized, timeout_seconds=timeout)
    if message is None:
        logger.warning(f"Crossref query failed for DOI {doi!r}")
    return message


# ── Enrichment orchestration ───────────────────────────────────────────

def enrich_from_doi(
    doi: str,
    *,
    chinese_title: str = "",
    query_crossref: bool = True,
) -> EnrichmentResult:
    """Enrich metadata from a DOI.

    If query_crossref is True (default), queries Crossref API for canonical metadata.
    Set to False for offline-only operation.
    """
    normalized = normalize_doi(doi)
    if not normalized:
        return EnrichmentResult(warnings=["invalid or empty DOI"])

    result = EnrichmentResult(doi=normalized, chinese_title=chinese_title)
    meta: dict = {}

    if query_crossref:
        raw = query_crossref_by_doi(normalized)
        if raw:
            meta = normalize_crossref_metadata(raw)
            result.confidence = 0.95
            result.source = "crossref"
            result.raw = raw
        else:
            result.warnings.append(f"Crossref query returned no data for {normalized}")
    else:
        result.source = "doi_only"
        result.confidence = 0.3

    if meta:
        result.doi = meta.get("doi") or result.doi
        result.title = meta.get("title", "")
        result.year = meta.get("year")
        result.authors = meta.get("authors", [])
        result.first_author = meta.get("first_author", "")
        result.venue = meta.get("venue", "")
        result.publisher = meta.get("publisher", "")
        result.volume = meta.get("volume", "")
        result.number = meta.get("number", "")
        result.issue = meta.get("issue", "")
        result.pages = meta.get("pages", "")
        result.article_number = meta.get("article_number", "")
        result.url = meta.get("url", "")
        result.issn = meta.get("issn", "")
        result.published = meta.get("published", "")
        if not result.source or result.source == "doi_only":
            result.source = meta.get("source", result.source)

    return result


def enrich_from_metadata_record(record: dict, chinese_title: str = "") -> EnrichmentResult:
    """Enrich metadata from a fetch/search metadata record."""
    doi = extract_doi_from_metadata_record(record) or record.get("doi", "")
    title = record.get("title", "") or ""
    year = record.get("year")
    authors = record.get("authors") or record.get("author") or []
    if isinstance(authors, str):
        authors = [authors]
    first_author = record.get("first_author", "")

    if doi:
        result = enrich_from_doi(doi, chinese_title=chinese_title)
        # Metadata record data may fill gaps if Crossref failed.
        if not result.title and title:
            result.title = title
        if result.year is None and year:
            try:
                result.year = int(year)
            except (TypeError, ValueError):
                pass
        if not result.authors and authors:
            result.authors = authors
        if not result.first_author and first_author:
            result.first_author = first_author
        elif not result.first_author and authors:
            result.first_author = authors[0] if isinstance(authors, list) else str(authors)
        if result.source == "doi_only" or not result.source:
            result.source = record.get("source_kind") or record.get("resolver") or "metadata_record"
        return result

    # No DOI — build from metadata record data only.
    if year is not None:
        try:
            year = int(year)
        except (TypeError, ValueError):
            year = None

    result = EnrichmentResult(
        title=str(title),
        year=year,
        authors=list(authors) if isinstance(authors, list) else [str(authors)] if authors else [],
        first_author=str(first_author) if first_author else (authors[0] if isinstance(authors, list) and authors else ""),
        source="metadata_record",
        confidence=0.4,
        chinese_title=chinese_title or record.get("chinese_title", ""),
        warnings=["no DOI in metadata record; metadata from record only"],
    )
    return result


def enrich_from_pdf(
    pdf_path: str | Path,
    *,
    sidecar: dict | None = None,
    chinese_title: str = "",
) -> EnrichmentResult:
    """Full enrichment pipeline for a v2 paper_raw PDF.

    1. Try DOI from fetch metadata record
    2. Try DOI from PDF filename
    3. Try DOI from PDF text (pymupdf)
    4. If DOI found, query Crossref
    5. Return EnrichmentResult
    """
    pdf_path = Path(pdf_path)
    sidecar = sidecar or {}
    warnings: list[str] = []

    # 1. Try fetch metadata record DOI first.
    doi = extract_doi_from_metadata_record(sidecar)
    if doi:
        result = enrich_from_doi(doi, chinese_title=chinese_title)
        # Merge metadata record fields.
        if not result.title and sidecar.get("title"):
            result.title = sidecar["title"]
        if result.year is None and sidecar.get("year"):
            try:
                result.year = int(sidecar["year"])
            except (TypeError, ValueError):
                pass
        if not result.authors and sidecar.get("authors"):
            authors = sidecar["authors"]
            if isinstance(authors, str):
                authors = [authors]
            result.authors = authors
            if not result.first_author:
                result.first_author = authors[0] if authors else ""
        result.chinese_title = chinese_title or sidecar.get("chinese_title", "")
        return result

    # 2. Try DOI from PDF filename
    doi = extract_doi_from_filename(pdf_path.name)
    if not doi:
        # 3. Try DOI from PDF text
        doi = extract_doi_from_pdf_file(pdf_path)
        if not doi:
            warnings.append("no DOI found in metadata record, filename, or PDF text")
        else:
            warnings.append("DOI extracted from PDF text (pymupdf)")
    else:
        warnings.append("DOI extracted from PDF filename")

    if doi:
        result = enrich_from_doi(doi, chinese_title=chinese_title)
        result.warnings.extend(warnings)
        return result

    # No DOI at all — use metadata record or filename metadata.
    result = EnrichmentResult(
        chinese_title=chinese_title,
        warnings=warnings,
        source="filename_fallback",
        confidence=0.1,
    )
    if sidecar:
        result.title = sidecar.get("title", "")
        result.year = sidecar.get("year")
        result.authors = sidecar.get("authors") or []
        result.first_author = sidecar.get("first_author", "")
        if not result.title:
            result.title = pdf_path.stem
            result.warnings.append("title fallback from filename stem")
    else:
        result.title = pdf_path.stem
        result.warnings.append("no metadata record, title from filename stem")

    return result


# ── Bad paper_name detection ─────────────────────────────────────────────

_BAD_FILENAME_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"^download$", r"^article$", r"^fulltext$", r"^paper$", r"^manuscript$",
        r"^untitled$", r"^document$", r"^pdf$", r"^main$", r"^output$",
        r"^science\.[a-z0-9]+$",
        r"^s\d{4}[-\s]?\d{4,}$",
        r"^j\.j[a-z]+\.\d{4,}",
        r"^1-s2\.0-",
        r"^10\.\d{4,}",
        r"^[a-z]+_\d{2}_\d+$",
    ]
]


def looks_like_bad_paper_name(paper_name: str) -> tuple[bool, str]:
    """Check if a paper_name looks like it was derived from a raw PDF filename.

    Returns (is_bad, reason).
    """
    if not paper_name or len(paper_name) < 3:
        return True, "too_short"

    for pat in _BAD_FILENAME_PATTERNS:
        if pat.search(paper_name):
            return True, f"matches_bad_pattern:{pat.pattern}"

    # Check for year_author_title pattern
    has_year = bool(re.match(r"^\d{4}_", paper_name))
    if not has_year:
        return True, "missing_year_prefix"

    parts = paper_name.split("_")
    if len(parts) < 3:
        return True, "too_few_parts"

    # Check for excessively long paper_name
    if len(paper_name) > 120:
        return True, "too_long"

    return False, ""
