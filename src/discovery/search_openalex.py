"""OpenAlex works search."""
from __future__ import annotations

from contextlib import nullcontext
from typing import Any, Literal

import requests
from loguru import logger

from src.discovery.models import PaperCandidate, normalize_doi
from src.discovery.provider_models import DiscoveryPage, classify_http_error, failed_page
from src.fetch.proxy import get_fetch_proxies
from src.services.openalex_credentials import (
    OpenAlexCredentials,
    load_openalex_credentials,
    safe_request_error_summary,
)


OPENALEX_WORKS_URL = "https://api.openalex.org/works"
OPENALEX_PROVIDER = "openalex"


def _headers(credentials: OpenAlexCredentials) -> dict[str, str]:
    headers = {"User-Agent": "mineru-literature-library/0.1"}
    if credentials.api_key:
        headers["Authorization"] = f"Bearer {credentials.api_key}"
    return headers


def _params(
    query: str, limit: int, credentials: OpenAlexCredentials
) -> dict[str, str | int]:
    params: dict[str, str | int] = {"search": query, "per-page": limit}
    if credentials.email:
        params["mailto"] = credentials.email
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
        credentials = load_openalex_credentials()
        logger.debug(credentials.safe_summary())
        response = requests.get(
            OPENALEX_WORKS_URL,
            params=_params(query, limit, credentials),
            headers=_headers(credentials),
            timeout=20,
            proxies=get_fetch_proxies(),
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        safe_error = safe_request_error_summary(exc)
        logger.warning("OpenAlex search failed for {!r}: {}", query, safe_error)
        return []
    return [
        parse_openalex_work(work, query=query, domain_id=domain_id)
        for work in data.get("results", [])
    ]


# ── Cursor-paginated page (Refresh/Backfill lanes) ──────────────────


def _page_params(
    query: str, page_size: int, cursor: str, credentials: OpenAlexCredentials
) -> dict[str, str | int]:
    params: dict[str, str | int] = {
        "search": query,
        "per-page": page_size,
        "cursor": cursor,
    }
    if credentials.email:
        params["mailto"] = credentials.email
    return params


def search_openalex_page(
    query: str,
    *,
    original_keyword: str,
    lane: Literal["refresh", "backfill"],
    page_size: int,
    cursor: str = "*",
    sort: str | None = None,
    domain_id: str | None = None,
    rate_limiter: Any | None = None,
    limiter_lock: Any | None = None,
) -> DiscoveryPage:
    """Fetch one OpenAlex works page via cursor pagination.

    - Refresh lane: caller passes ``cursor="*"`` (first page).
    - Backfill lane: caller passes the saved cursor.

    On HTTP failure the returned page has ``status="failed"`` and
    ``next_cursor=None`` so the caller does NOT advance the backfill
    cursor. On success with a null/empty ``next_cursor`` the page is
    marked ``exhausted=True``.
    """
    credentials = load_openalex_credentials()
    params = _page_params(query, page_size, cursor, credentials)
    if sort:
        params["sort"] = sort

    lock_ctx = limiter_lock if limiter_lock is not None else nullcontext()
    try:
        if rate_limiter is not None:
            with lock_ctx:
                rate_limiter.wait(OPENALEX_PROVIDER)
        response = requests.get(
            OPENALEX_WORKS_URL,
            params=params,
            headers=_headers(credentials),
            timeout=20,
            proxies=get_fetch_proxies(),
        )
        if rate_limiter is not None:
            with lock_ctx:
                rate_limiter.record_response(
                    OPENALEX_PROVIDER,
                    dict(response.headers),
                    response.status_code,
                )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        safe_error = safe_request_error_summary(exc)
        logger.warning(
            "OpenAlex page failed for {!r} (cursor={!r}): {}",
            query, cursor, safe_error,
        )
        _error_type, failure_class, http_status, retry_after = classify_http_error(exc)
        return failed_page(
            provider=OPENALEX_PROVIDER,
            original_keyword=original_keyword,
            expanded_query=query,
            lane=lane,
            request_cursor=cursor,
            page_size=page_size,
            error_type=_error_type,
            safe_error=safe_error,
            failure_class=failure_class,
            http_status=http_status,
            retry_after_seconds=retry_after,
        )

    results = data.get("results", []) or []
    meta = data.get("meta") or {}
    next_cursor = meta.get("next_cursor")
    total_results = meta.get("count")
    if next_cursor and str(next_cursor) == str(cursor):
        return failed_page(
            provider=OPENALEX_PROVIDER,
            original_keyword=original_keyword,
            expanded_query=query,
            lane=lane,
            request_cursor=cursor,
            page_size=page_size,
            error_type="cursor_not_advancing",
            safe_error="OpenAlex next_cursor did not advance",
            failure_class="terminal",
        )
    candidates = [
        parse_openalex_work(work, query=query, domain_id=domain_id)
        for work in results
    ]
    exhausted = (not results) or (not next_cursor)
    return DiscoveryPage(
        provider=OPENALEX_PROVIDER,
        original_keyword=original_keyword,
        expanded_query=query,
        lane=lane,
        candidates=candidates,
        request_cursor=cursor,
        next_cursor=next_cursor if next_cursor else None,
        page_size=page_size,
        returned_count=len(candidates),
        total_results=total_results,
        status="success",
        exhausted=exhausted,
    )
