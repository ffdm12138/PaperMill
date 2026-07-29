"""Ranked OA PDF candidates from provider location lists.

Unpaywall, OpenAlex, and Semantic Scholar all report *several* places a paper
can be downloaded from: the publisher's own copy plus any repository copies
(institutional archives, DOAJ, preprint servers).  Each provider ranks the
publisher copy first — Unpaywall's ``best_oa_location`` is almost always the
publisher.

That ranking is wrong for this project.  When the publisher host refuses the
current egress (see ``src/fetch/host_policy``) the publisher copy is the one
URL guaranteed to fail, while a repository copy of the same article downloads
without trouble.  Collapsing a provider's location list down to a single URL —
and picking the publisher's — is what makes an otherwise-available paper look
unobtainable.

This module normalizes any provider's location list into a deduplicated,
ranked ``list[PdfCandidate]``.  Resolvers attach the full list to
``FetchResult.pdf_candidates`` and the pipeline walks it until one yields real
PDF bytes.

Ranking, best first:

1. hosts that are not known-blocked, before hosts that are;
2. repository copies, before publisher copies, before unknown host types;
3. explicit PDF links (``url_for_pdf``), before landing pages;
4. published version, before accepted manuscript, before submitted;
5. original provider order, as a stable tie-break.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from src.fetch.host_policy import is_bot_blocked_host, normalize_host


#: ``host_type`` values, best first. Repository copies live on university and
#: archive servers that do not run publisher bot management.
_HOST_TYPE_RANK = {"repository": 0, "publisher": 1}
_HOST_TYPE_UNKNOWN_RANK = 2

#: ``version`` values, best first. Anything else sorts last.
_VERSION_RANK = {
    "publishedversion": 0,
    "acceptedversion": 1,
    "submittedversion": 2,
}
_VERSION_UNKNOWN_RANK = 3


@dataclass(frozen=True)
class PdfCandidate:
    """One downloadable location for a paper."""

    url: str
    host_type: str = ""
    version: str = ""
    license: str = ""
    is_direct_pdf: bool = False
    source: str = ""
    order: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def host(self) -> str:
        return normalize_host(self.url)

    @property
    def is_blocked_host(self) -> bool:
        return is_bot_blocked_host(self.url)

    def sort_key(self) -> tuple:
        return (
            1 if self.is_blocked_host else 0,
            _HOST_TYPE_RANK.get((self.host_type or "").strip().lower(), _HOST_TYPE_UNKNOWN_RANK),
            0 if self.is_direct_pdf else 1,
            _VERSION_RANK.get((self.version or "").strip().lower(), _VERSION_UNKNOWN_RANK),
            self.order,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a persistence-safe view (no raw provider payload)."""
        return {
            "url": self.url,
            "host": self.host,
            "host_type": self.host_type,
            "version": self.version,
            "license": self.license,
            "is_direct_pdf": self.is_direct_pdf,
            "source": self.source,
            "blocked_host": self.is_blocked_host,
        }


def rank_candidates(candidates: Iterable[PdfCandidate]) -> list[PdfCandidate]:
    """Return *candidates* deduplicated by URL and sorted best-first.

    The first occurrence of a URL wins, so a location that arrives with richer
    fields earlier in the provider's own list keeps them.
    """
    seen: dict[str, PdfCandidate] = {}
    for index, candidate in enumerate(candidates):
        url = str(candidate.url or "").strip()
        if not url or url in seen:
            continue
        seen[url] = candidate if candidate.order else _with_order(candidate, index)
    return sorted(seen.values(), key=lambda item: item.sort_key())


def _with_order(candidate: PdfCandidate, order: int) -> PdfCandidate:
    return PdfCandidate(
        url=candidate.url,
        host_type=candidate.host_type,
        version=candidate.version,
        license=candidate.license,
        is_direct_pdf=candidate.is_direct_pdf,
        source=candidate.source,
        order=order,
        extra=candidate.extra,
    )


def candidates_from_unpaywall(data: Mapping[str, Any], *, source: str = "unpaywall") -> list[PdfCandidate]:
    """Build ranked candidates from an Unpaywall ``/v2/{doi}`` payload.

    Both ``url_for_pdf`` and ``url`` of every location are kept: the former is
    a direct PDF, the latter a landing page the pipeline can still parse.
    """
    locations: list[Mapping[str, Any]] = []
    best = data.get("best_oa_location")
    if isinstance(best, Mapping):
        locations.append(best)
    for location in data.get("oa_locations") or []:
        if isinstance(location, Mapping):
            locations.append(location)

    fallback_license = str(data.get("license") or "")
    out: list[PdfCandidate] = []
    for order, location in enumerate(locations):
        host_type = str(location.get("host_type") or "")
        version = str(location.get("version") or "")
        license_name = str(location.get("license") or fallback_license)
        for url, direct in (
            (location.get("url_for_pdf"), True),
            (location.get("url"), False),
        ):
            text = str(url or "").strip()
            if text:
                out.append(PdfCandidate(
                    url=text,
                    host_type=host_type,
                    version=version,
                    license=license_name,
                    is_direct_pdf=direct,
                    source=source,
                    order=order,
                ))
    return rank_candidates(out)


def candidates_from_openalex(work: Mapping[str, Any], *, source: str = "openalex") -> list[PdfCandidate]:
    """Build ranked candidates from an OpenAlex work.

    OpenAlex exposes ``locations[]`` alongside ``primary_location`` and
    ``best_oa_location``; only the open ones are usable here.
    """
    seen_locations: list[Mapping[str, Any]] = []
    for key in ("best_oa_location", "primary_location"):
        location = work.get(key)
        if isinstance(location, Mapping):
            seen_locations.append(location)
    for location in work.get("locations") or []:
        if isinstance(location, Mapping):
            seen_locations.append(location)

    out: list[PdfCandidate] = []
    for order, location in enumerate(seen_locations):
        if location.get("is_oa") is False:
            continue
        host_type = _openalex_host_type(location)
        version = str(location.get("version") or "")
        license_name = str(location.get("license") or "")
        for url, direct in (
            (location.get("pdf_url"), True),
            (location.get("landing_page_url"), False),
        ):
            text = str(url or "").strip()
            if text:
                out.append(PdfCandidate(
                    url=text,
                    host_type=host_type,
                    version=version,
                    license=license_name,
                    is_direct_pdf=direct,
                    source=source,
                    order=order,
                ))

    open_access = work.get("open_access")
    if isinstance(open_access, Mapping):
        oa_url = str(open_access.get("oa_url") or "").strip()
        if oa_url:
            out.append(PdfCandidate(
                url=oa_url,
                host_type="",
                source=source,
                order=len(seen_locations),
            ))
    return rank_candidates(out)


def _openalex_host_type(location: Mapping[str, Any]) -> str:
    """Map an OpenAlex location onto Unpaywall's ``host_type`` vocabulary."""
    explicit = str(location.get("host_type") or "").strip().lower()
    if explicit:
        return explicit
    source_info = location.get("source")
    source_type = ""
    if isinstance(source_info, Mapping):
        source_type = str(source_info.get("type") or "").strip().lower()
    if source_type == "repository":
        return "repository"
    if source_type in {"journal", "conference", "book series", "ebook platform"}:
        return "publisher"
    return ""


def candidates_from_semantic_scholar(data: Mapping[str, Any], *, source: str = "semantic_scholar") -> list[PdfCandidate]:
    """Build ranked candidates from a Semantic Scholar paper record."""
    pdf = data.get("openAccessPdf")
    if not isinstance(pdf, Mapping):
        return []
    url = str(pdf.get("url") or "").strip()
    if not url:
        return []
    return rank_candidates([PdfCandidate(
        url=url,
        host_type="",
        license=str(pdf.get("license") or ""),
        is_direct_pdf=True,
        source=source,
    )])


def candidate_urls(candidates: Iterable[PdfCandidate]) -> list[str]:
    """Return just the URLs, preserving candidate order."""
    return [candidate.url for candidate in candidates]


def all_candidates_blocked(candidates: Iterable[PdfCandidate]) -> bool:
    """Return True when there is at least one candidate and all are blocked."""
    items = list(candidates)
    return bool(items) and all(item.is_blocked_host for item in items)
