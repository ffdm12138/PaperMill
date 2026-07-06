"""OpenAlex works search."""
import os

import requests
from loguru import logger

from src.discovery.models import PaperCandidate, normalize_doi
from src.fetch.proxy import get_fetch_proxies


OPENALEX_WORKS_URL = "https://api.openalex.org/works"


def _headers() -> dict[str, str]:
    headers = {"User-Agent": "mineru-literature-library/0.1"}
    api_key = os.environ.get("OPENALEX_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _params(query: str, limit: int) -> dict[str, str | int]:
    params: dict[str, str | int] = {"search": query, "per-page": limit}
    email = os.environ.get("OPENALEX_EMAIL", "").strip()
    if email:
        params["mailto"] = email
    return params


def _authors(work: dict) -> list[str]:
    names = []
    for authorship in work.get("authorships") or []:
        author = authorship.get("author") or {}
        name = author.get("display_name")
        if name:
            names.append(name)
    return names


def _pdf_url(work: dict) -> str:
    primary = work.get("primary_location") or {}
    if primary.get("pdf_url"):
        return primary["pdf_url"]
    open_access = work.get("open_access") or {}
    return open_access.get("oa_url") or ""


def parse_openalex_work(work: dict, query: str = "", domain_id: str | None = None) -> PaperCandidate:
    title = work.get("display_name") or work.get("title") or ""
    host = ((work.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
    open_access = work.get("open_access") or {}
    return PaperCandidate(
        title=title,
        year=work.get("publication_year"),
        authors=_authors(work),
        doi=normalize_doi(work.get("doi")),
        venue=host,
        abstract="",
        source="openalex",
        source_id=work.get("id") or "",
        url=work.get("id") or "",
        pdf_url=_pdf_url(work),
        open_access=bool(open_access.get("is_oa")),
        citation_count=work.get("cited_by_count"),
        query=query,
        domain_id=domain_id,
        raw=work,
    )


def search_openalex(query: str, domain_id: str | None = None, limit: int = 25) -> list[PaperCandidate]:
    try:
        response = requests.get(
            OPENALEX_WORKS_URL,
            params=_params(query, limit),
            headers=_headers(),
            timeout=20,
            proxies=get_fetch_proxies(),
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.warning(f"OpenAlex search failed for {query!r}: {exc}")
        return []
    return [
        parse_openalex_work(work, query=query, domain_id=domain_id)
        for work in data.get("results", [])
    ]

