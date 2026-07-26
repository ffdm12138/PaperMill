"""Provider raw-record canonicalization for network metadata staging."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.utils.identifiers import normalize_doi


TYPE_MAP = {
    "journal-article": "article",
    "proceedings-article": "inproceedings",
    "edited-book": "book",
    "monograph": "book",
    "book": "book",
    "book-chapter": "incollection",
    "posted-content": "preprint",
    "dataset": "dataset",
    "article": "article",
    "preprint": "preprint",
    # OpenAlex-specific types with clear BibTeX equivalents.
    "review": "article",
    "editorial": "article",
    "letter": "article",
    "erratum": "article",
    "dissertation": "phdthesis",
    "report": "techreport",
    "reference-entry": "incollection",
    "standard": "misc",
    "paratext": "misc",
    "supplementary-materials": "misc",
    "other": "misc",
}


@dataclass
class CanonicalBibliographicRecord:
    entry_type: str = "misc"
    title: str = ""
    year: int | None = None
    authors: list[dict[str, str]] = field(default_factory=list)
    doi: str = ""
    venue: str = ""
    publisher: str = ""
    volume: str = ""
    issue: str = ""
    number: str = ""
    pages: str = ""
    article_number: str = ""
    issn: list[str] = field(default_factory=list)
    isbn: list[str] = field(default_factory=list)
    url: str = ""
    pdf_url: str = ""
    published: str = ""
    online: str = ""
    warnings: list[str] = field(default_factory=list)


def _first(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0] if value else "").strip()
    return str(value or "").strip()


def _pick_text(*values: Any) -> str:
    """Return the first non-empty stripped string value, or ``''``."""
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def combine_page_range(first_page: Any, last_page: Any) -> str:
    """Combine OpenAlex ``biblio.first_page``/``last_page`` into a page range.

    Rules:
        first=101, last=110 → "101-110"
        first=101, last=""  → "101"
        first="",  last=110 → "110"
        first=101, last=101 → "101"
        both empty           → ""

    Never produces ``None-None``, ``101-``, or ``-110``.
    """
    first = str(first_page or "").strip()
    last = str(last_page or "").strip()
    if not first and not last:
        return ""
    if first and last and first != last:
        return f"{first}-{last}"
    return first or last


_OA_DATE_RE = __import__("re").compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


def _normalize_oa_date(value: Any) -> str:
    """Validate an OpenAlex ``publication_date`` string (YYYY[-MM[-DD]]).

    Returns the stripped string when it matches, ``''`` otherwise. OpenAlex
    dates are ISO-ish strings, not Crossref date-parts arrays.
    """
    text = str(value or "").strip()
    if text and _OA_DATE_RE.match(text):
        return text
    return ""


def _year_from_oa_date(value: Any) -> int | None:
    text = _normalize_oa_date(value)
    if not text:
        return None
    try:
        return int(text.split("-", 1)[0])
    except (TypeError, ValueError):
        return None


def _year_from_date_parts(record: dict[str, Any]) -> int | None:
    for key in ("published-online", "published-print", "issued"):
        parts = (((record.get(key) or {}).get("date-parts") or [[]])[0])
        if parts:
            try:
                return int(parts[0])
            except (TypeError, ValueError):
                return None
    return None


def _date_from_parts(record: dict[str, Any], key: str) -> str:
    parts = (((record.get(key) or {}).get("date-parts") or [[]])[0])
    if not parts:
        return ""
    return "-".join(f"{int(part):02d}" if idx else str(int(part)) for idx, part in enumerate(parts) if part is not None)


def _stable_values(*values: Any) -> list[str]:
    out: list[str] = []
    for value in values:
        if isinstance(value, list):
            items = value
        elif value:
            items = [value]
        else:
            items = []
        for item in items:
            text = str(item or "").strip()
            if text and text not in out:
                out.append(text)
    return sorted(out)


def _crossref_authors(record: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for author in record.get("author") or []:
        if not isinstance(author, dict):
            continue
        given = str(author.get("given") or "").strip()
        family = str(author.get("family") or "").strip()
        full = " ".join(part for part in (given, family) if part).strip() or str(author.get("name") or "").strip()
        affiliations = author.get("affiliation") or []
        affiliation = ""
        if affiliations and isinstance(affiliations[0], dict):
            affiliation = str(affiliations[0].get("name") or "").strip()
        out.append({
            "full_name": full,
            "family": family,
            "given": given,
            "orcid": str(author.get("ORCID") or author.get("orcid") or "").strip(),
            "affiliation": affiliation,
        })
    return [a for a in out if a["full_name"] or a["family"]]


def _openalex_authors(record: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for authorship in record.get("authorships") or []:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author") or {}
        name = str(author.get("display_name") or "").strip()
        family = name.split()[-1] if name else ""
        affiliations = authorship.get("institutions") or []
        affiliation = ""
        if affiliations and isinstance(affiliations[0], dict):
            affiliation = str(affiliations[0].get("display_name") or affiliations[0].get("name") or "").strip()
        out.append({
            "full_name": name,
            "family": family,
            "given": " ".join(name.split()[:-1]) if name else "",
            "orcid": str(author.get("orcid") or "").strip(),
            "affiliation": affiliation,
        })
    return [a for a in out if a["full_name"] or a["family"]]


def _flat_authors(record: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for author in record.get("authors") or []:
        if isinstance(author, dict):
            full = str(author.get("full_name") or author.get("name") or author.get("display_name") or "").strip()
            family = str(author.get("family") or "").strip() or (full.split()[-1] if full else "")
            out.append({
                "full_name": full,
                "family": family,
                "given": str(author.get("given") or "").strip(),
                "orcid": str(author.get("orcid") or "").strip(),
                "affiliation": str(author.get("affiliation") or "").strip(),
            })
        else:
            full = str(author or "").strip()
            out.append({"full_name": full, "family": full.split()[-1] if full else "", "given": "", "orcid": "", "affiliation": ""})
    return [a for a in out if a["full_name"] or a["family"]]


def _entry_type(raw_type: str, warnings: list[str]) -> str:
    key = str(raw_type or "").strip().lower()
    if not key:
        warnings.append("missing provider type; using misc")
        return "misc"
    if key in TYPE_MAP:
        return TYPE_MAP[key]
    warnings.append(f"unknown provider type {key!r}; using misc")
    return "misc"


def _merge_author_list(primary: list[dict[str, str]], enrichment: list[dict[str, str]]) -> list[dict[str, str]]:
    if not primary:
        return enrichment
    merged: list[dict[str, str]] = []
    for idx, author in enumerate(primary):
        item = dict(author)
        extra = enrichment[idx] if idx < len(enrichment) else {}
        for key in ("orcid", "affiliation"):
            if not item.get(key) and extra.get(key):
                item[key] = extra[key]
        merged.append(item)
    return merged


def canonicalize_network_record(record: dict[str, Any]) -> CanonicalBibliographicRecord:
    raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
    resolution = record.get("doi_resolution") if isinstance(record.get("doi_resolution"), dict) else {}
    resolution_raw = resolution.get("raw_record") if isinstance(resolution.get("raw_record"), dict) else {}
    provider = str(record.get("provider") or record.get("source") or "").lower()
    candidate_raw = raw
    warnings: list[str] = []

    # OpenAlex stores volume/issue/pages inside a nested ``biblio`` object and
    # uses ``publication_date`` (ISO string) instead of Crossref date-parts.
    # Crossref resolution raw remains bibliographically primary per-field;
    # OpenAlex candidate raw fills volume/issue/pages/date when Crossref is
    # absent or silent. The flattened record is the last-resort fallback.
    oa_biblio = candidate_raw.get("biblio") if isinstance(candidate_raw.get("biblio"), dict) else {}
    biblio = resolution_raw or candidate_raw
    oa_authors = _openalex_authors(candidate_raw)
    cr_authors = _crossref_authors(resolution_raw) or _crossref_authors(candidate_raw)
    authors = _merge_author_list(cr_authors, oa_authors) if cr_authors else (oa_authors or _flat_authors(record))

    primary_location = candidate_raw.get("primary_location") or {}
    open_access = candidate_raw.get("open_access") or {}
    resolution_pdf_url = ""
    links = resolution_raw.get("link")
    if isinstance(links, list) and links and isinstance(links[0], dict):
        resolution_pdf_url = str(links[0].get("URL") or "")
    pdf_url = (
        record.get("pdf_url")
        or primary_location.get("pdf_url")
        or open_access.get("oa_url")
        or resolution_pdf_url
    )
    source = (candidate_raw.get("primary_location") or {}).get("source") or {}
    venue = (
        _first(biblio.get("container-title"))
        or source.get("display_name")
        or record.get("venue")
        or record.get("journal")
        or record.get("container_title")
        or ""
    )
    entry_type = _entry_type(biblio.get("type") or candidate_raw.get("type") or record.get("type"), warnings)
    year = (
        _year_from_date_parts(resolution_raw)
        or _year_from_date_parts(candidate_raw)
        or _year_from_oa_date(candidate_raw.get("publication_date"))
        or record.get("year")
        or record.get("publication_year")
        or candidate_raw.get("publication_year")
    )
    try:
        year = int(year) if year is not None else None
    except (TypeError, ValueError):
        year = None

    # Per-field merge: Crossref resolution first, then candidate_raw top-level
    # (Crossref-style fields when the candidate itself is a Crossref record),
    # then OpenAlex biblio sub-object, then flattened record. Pages combine
    # OpenAlex first_page/last_page when present.
    volume = _pick_text(
        resolution_raw.get("volume"),
        candidate_raw.get("volume"),
        oa_biblio.get("volume"),
        record.get("volume"),
    )
    issue = _pick_text(
        resolution_raw.get("issue"),
        candidate_raw.get("issue"),
        oa_biblio.get("issue"),
        record.get("issue"),
    )
    oa_pages = combine_page_range(oa_biblio.get("first_page"), oa_biblio.get("last_page"))
    pages = _pick_text(
        resolution_raw.get("page"),
        candidate_raw.get("page"),
        oa_pages,
        record.get("page"),
        record.get("pages"),
    )
    article_number = _pick_text(
        resolution_raw.get("article-number"),
        candidate_raw.get("article-number"),
        oa_biblio.get("article_number"),
        record.get("article-number"),
        record.get("article_number"),
    )
    number = _pick_text(
        resolution_raw.get("number"),
        resolution_raw.get("issue"),
        candidate_raw.get("number"),
        candidate_raw.get("issue"),
        oa_biblio.get("issue"),
        record.get("number"),
        record.get("issue"),
    )
    published = _pick_text(
        _date_from_parts(resolution_raw, "issued"),
        _date_from_parts(candidate_raw, "issued"),
        _normalize_oa_date(candidate_raw.get("publication_date")),
    )
    online = _pick_text(
        _date_from_parts(resolution_raw, "published-online"),
        _date_from_parts(candidate_raw, "published-online"),
    )

    return CanonicalBibliographicRecord(
        entry_type=entry_type,
        title=_first(biblio.get("title")) or record.get("title") or record.get("display_name") or candidate_raw.get("display_name") or "",
        year=year,
        authors=authors,
        doi=normalize_doi(record.get("doi") or biblio.get("DOI") or candidate_raw.get("doi") or ""),
        venue=str(venue or ""),
        publisher=_pick_text(
            biblio.get("publisher"),
            source.get("host_organization_name"),
        ),
        volume=volume,
        issue=issue,
        number=number,
        pages=pages,
        article_number=article_number,
        issn=_stable_values(
            resolution_raw.get("ISSN"),
            resolution_raw.get("issn"),
            candidate_raw.get("ISSN"),
            candidate_raw.get("issn"),
            record.get("issn"),
        ),
        isbn=_stable_values(
            resolution_raw.get("ISBN"),
            resolution_raw.get("isbn"),
            candidate_raw.get("ISBN"),
            candidate_raw.get("isbn"),
            record.get("isbn"),
        ),
        url=_pick_text(
            record.get("url"),
            biblio.get("URL"),
            candidate_raw.get("id"),
        ),
        pdf_url=str(pdf_url or ""),
        published=published,
        online=online,
        warnings=warnings,
    )
