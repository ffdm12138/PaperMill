"""Crossref DOI and BibTeX verification."""
from difflib import SequenceMatcher

import requests
from loguru import logger

from src.discovery.models import PaperCandidate, normalize_doi, normalize_title
from src.fetch.proxy import get_fetch_proxies


CROSSREF_WORKS_URL = "https://api.crossref.org/works"


def _year(message: dict) -> int | None:
    for key in ("published-print", "published-online", "issued"):
        parts = (((message.get(key) or {}).get("date-parts") or [[]])[0])
        if parts:
            try:
                return int(parts[0])
            except (TypeError, ValueError):
                return None
    return None


def _authors(message: dict) -> list[str]:
    out = []
    for author in message.get("author") or []:
        name = " ".join(filter(None, [author.get("given"), author.get("family")])).strip()
        if name:
            out.append(name)
    return out


def parse_crossref_item(item: dict, query: str = "", domain_id: str | None = None) -> PaperCandidate:
    title = (item.get("title") or [""])[0]
    container = (item.get("container-title") or [""])[0]
    return PaperCandidate(
        title=title,
        year=_year(item),
        authors=_authors(item),
        doi=item.get("DOI") or "",
        venue=container,
        source="crossref",
        source_id=item.get("DOI") or "",
        url=item.get("URL") or "",
        citation_count=item.get("is-referenced-by-count"),
        query=query,
        domain_id=domain_id,
        raw=item,
    )


def search_crossref(query: str, domain_id: str | None = None, limit: int = 5) -> list[PaperCandidate]:
    try:
        response = requests.get(
            CROSSREF_WORKS_URL,
            params={"query.bibliographic": query, "rows": limit},
            timeout=20,
            proxies=get_fetch_proxies(),
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.warning(f"Crossref search failed for {query!r}: {exc}")
        return []
    items = (data.get("message") or {}).get("items") or []
    return [parse_crossref_item(item, query=query, domain_id=domain_id) for item in items]


def resolve_crossref_by_title(
    title: str,
    year: int | None = None,
    limit: int = 5,
    domain_id: str | None = None,
) -> list[PaperCandidate]:
    """按标题相似度 + 年份接近度对 Crossref 候选排序，返回完整候选列表。

    网络错误时返回空列表（由 ``search_crossref`` 吞咽）。不做阈值过滤——
    阈值过滤由 ``resolve_doi_by_title`` 负责。
    """
    candidates = search_crossref(title, domain_id=domain_id, limit=limit)
    title_norm = normalize_title(title)
    scored: list[tuple[float, PaperCandidate]] = []
    for candidate in candidates:
        score = SequenceMatcher(None, title_norm, normalize_title(candidate.title)).ratio()
        if year and candidate.year:
            score += 0.15 if abs(candidate.year - year) <= 1 else -0.1
        candidate.confidence = min(1.0, max(0.0, score))
        scored.append((score, candidate))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [candidate for _, candidate in scored]


def resolve_doi_by_title(title: str, year: int | None = None, domain_id: str | None = None) -> PaperCandidate | None:
    candidates = resolve_crossref_by_title(title, year=year, limit=5, domain_id=domain_id)
    if not candidates:
        return None
    best = candidates[0]
    if best.confidence >= 0.75:
        return best
    return None


def get_crossref_work_by_doi(doi: str) -> dict | None:
    """按 DOI 取 Crossref work 的 message dict，网络错误返回 None。"""
    doi = normalize_doi(doi)
    if not doi:
        return None
    try:
        response = requests.get(f"{CROSSREF_WORKS_URL}/{doi}", timeout=20, proxies=get_fetch_proxies())
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.warning(f"Crossref work lookup failed for {doi!r}: {exc}")
        return None
    return data.get("message") if isinstance(data, dict) else None


def get_bibtex_by_doi(doi: str) -> str:
    doi = doi.strip()
    if not doi:
        return ""
    try:
        response = requests.get(
            f"{CROSSREF_WORKS_URL}/{doi}/transform/application/x-bibtex",
            timeout=20,
            proxies=get_fetch_proxies(),
        )
        response.raise_for_status()
        return response.text.strip()
    except Exception as exc:
        logger.warning(f"Crossref BibTeX lookup failed for {doi!r}: {exc}")
        return ""

