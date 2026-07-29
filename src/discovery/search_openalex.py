"""OpenAlex works search.

All HTTP goes through the unified :class:`~src.discovery.provider_client.ProviderClient`
(limiter + retry + backoff + circuit breaker + telemetry).  This module only
builds request specs and parses responses — it never imports ``requests``.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from loguru import logger

from src.discovery.models import PaperCandidate
from src.utils.identifiers import normalize_doi
from src.discovery.execution.lane_models import LaneExecutionSpec
from src.discovery.providers.provider_client import ProviderClient, ProviderRuntime, RequestSpec
from src.discovery.providers.provider_errors import (
    ProviderError,
    ProviderPermanentError,
    ProviderProtocolError,
    ProviderRequestBudgetExhausted,
)
from src.discovery.providers.provider_models import DiscoveryPage, failed_page
from src.fetch.openalex_credentials import (
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


def _abstract_from_inverted_index(work: dict) -> str:
    inverted = work.get("abstract_inverted_index")
    if not isinstance(inverted, dict):
        return ""
    words: list[tuple[int, str]] = []
    for token, positions in inverted.items():
        if not isinstance(token, str) or not isinstance(positions, list):
            continue
        for position in positions:
            if isinstance(position, int) and position >= 0:
                words.append((position, token))
    return " ".join(token for _, token in sorted(words))


def _landing_url(work: dict) -> str:
    """Return the article's landing page, never the OpenAlex entity URI.

    ``work["id"]`` is ``https://openalex.org/W…`` — an identifier for the
    record, not a page that hosts the article.  Publishing it as the paper's
    URL sends every downstream consumer (citation ``URL`` field, the
    ``original_link`` PDF resolver) to an OpenAlex SPA shell that can never
    contain a PDF.  Prefer the real landing page, then the DOI resolver.
    """
    for key in ("primary_location", "best_oa_location"):
        location = work.get(key) or {}
        if isinstance(location, dict):
            landing = str(location.get("landing_page_url") or "").strip()
            if landing:
                return landing
    doi = normalize_doi(work.get("doi"))
    return f"https://doi.org/{doi}" if doi else ""


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
        abstract=_abstract_from_inverted_index(work),
        source="openalex",
        source_id=work.get("id") or "",
        url=_landing_url(work),
        pdf_url=_pdf_url(work),
        open_access=bool(open_access.get("is_oa")),
        citation_count=work.get("cited_by_count"),
        query=query,
        domain_id=domain_id,
        raw=work,
    )


def _runtime_client(client: ProviderClient | None) -> ProviderClient:
    """Return the injected client or the process-wide shared one.

    Batch execution paths MUST inject a client via ``runtime.provider_client()``
    so that telemetry and request budgets are batch-scoped.  The singleton
    fallback is retained for standalone operations (metadata resolution,
    profile building) that are not part of a discovery batch.
    """
    if client is not None:
        return client
    return ProviderRuntime.get().client(OPENALEX_PROVIDER)


def search_openalex(
    query: str,
    domain_id: str | None = None,
    limit: int = 25,
    *,
    client: ProviderClient | None = None,
) -> list[PaperCandidate]:
    credentials = load_openalex_credentials()
    logger.debug(credentials.safe_summary())
    spec = RequestSpec(
        provider=OPENALEX_PROVIDER,
        purpose="metadata_resolution",
        url=OPENALEX_WORKS_URL,
        params=_params(query, limit, credentials),
        headers=_headers(credentials),
        timeout_seconds=20,
    )
    try:
        outcome = _runtime_client(client).execute(spec)
        data = outcome.json()
    except ProviderRequestBudgetExhausted:
        # This is a clean batch valve, never a provider failure page.
        raise
    except ProviderError as exc:
        logger.warning("OpenAlex search failed for {!r}: {}", query, type(exc).__name__)
        return []
    return [
        parse_openalex_work(work, query=query, domain_id=domain_id)
        for work in data.get("results", [])
    ]


# ── Cursor-paginated page (Refresh/Backfill lanes) ──────────────────


def _page_params(
    query: str, page_size: int, cursor: str, credentials: OpenAlexCredentials,
    combined_filter: str = "",
) -> dict[str, str | int]:
    params: dict[str, str | int] = {
        "search": query,
        "per-page": page_size,
        "cursor": cursor,
    }
    if credentials.email:
        params["mailto"] = credentials.email
    if combined_filter:
        params["filter"] = combined_filter
    return params


def search_openalex_page(
    lane_spec: LaneExecutionSpec,
    cursor: str,
    client: ProviderClient,
) -> DiscoveryPage:
    """Fetch one OpenAlex works page via cursor pagination.

    The immutable execution spec is the sole request identity.  This adapter
    accepts no loose query/filter/generation parameters and cannot silently
    fall back to a process-global client during a discovery batch.

    All HTTP goes through the unified :class:`ProviderClient` (limiter,
    retry, backoff, circuit breaker, telemetry).  On provider failure the
    returned page has ``status="failed"`` and ``next_cursor=None`` so the
    caller does NOT advance the backfill cursor. On success with a
    null/empty ``next_cursor`` the page is marked ``exhausted=True``.

    """
    if lane_spec.key.provider != OPENALEX_PROVIDER:
        raise ValueError("search_openalex_page requires an OpenAlex LaneExecutionSpec")
    query = lane_spec.query
    keyword_zh = lane_spec.keyword_zh
    query_id = lane_spec.key.query_id
    query_language = lane_spec.query_language
    lane = lane_spec.key.mode
    stable_lane_id = lane_spec.key.stable_id()
    page_size = lane_spec.page_size
    sort = lane_spec.sort or None
    topic_filter = lane_spec.topic_filter
    credentials = load_openalex_credentials()
    if sort and "relevance" + "_score:" in sort and not str(query or "").strip():
        raise ValueError("OpenAlex relevance" + "_score sort requires a non-empty search query")
    if sort:
        allowed_sorts = {
            "relevance" + "_score:asc", "relevance" + "_score:desc",
            "cited_by_count:asc", "cited_by_count:desc",
            "publication_date:asc", "publication_date:desc",
        }
        if any(token.strip() not in allowed_sorts for token in sort.split(",")):
            raise ValueError(f"invalid OpenAlex sort: {sort!r}")
    params = _page_params(query, page_size, cursor, credentials, combined_filter=topic_filter)
    if sort:
        params["sort"] = sort

    spec = RequestSpec(
        provider=OPENALEX_PROVIDER,
        purpose="discovery_page",
        url=OPENALEX_WORKS_URL,
        params=params,
        headers=_headers(credentials),
        timeout_seconds=20,
        telemetry_tags={
            "lane_id": stable_lane_id,
            "query_id": query_id,
            "keyword_id": lane_spec.key.keyword_id,
            "mode": lane,
        },
    )
    try:
        outcome = client.execute(spec)
        data = outcome.json()
    except ProviderProtocolError as exc:
        logger.warning("OpenAlex page protocol error for {!r}: {}", query, exc)
        return failed_page(
            provider=OPENALEX_PROVIDER,
            keyword_zh=keyword_zh,
            query_id=query_id,
            query=query,
            query_language=query_language,
            lane=lane,
            request_cursor=cursor,
            page_size=page_size,
            error_type="protocol_error",
            safe_error=str(exc)[:200],
            failure_class="retryable",
        )
    except ProviderRequestBudgetExhausted:
        raise
    except ProviderError as exc:
        safe_error = f"{type(exc).__name__}: {exc}"[:200]
        logger.warning(
            "OpenAlex page failed for {!r} (cursor={!r}): {}",
            query, cursor, safe_error,
        )
        failure_class = "retryable" if exc.retryable else "terminal"
        http_status = getattr(exc, "http_status", None)
        retry_after = getattr(exc, "retry_after_seconds", None)
        return failed_page(
            provider=OPENALEX_PROVIDER,
            keyword_zh=keyword_zh,
            query_id=query_id,
            query=query,
            query_language=query_language,
            lane=lane,
            request_cursor=cursor,
            page_size=page_size,
            error_type=type(exc).__name__,
            safe_error=safe_error,
            failure_class=failure_class,
            http_status=http_status,
            retry_after_seconds=retry_after,
        )
    response_body = outcome.body

    results = data.get("results", []) or []
    meta = data.get("meta") or {}
    next_cursor = meta.get("next_cursor")
    total_results = meta.get("count")
    if next_cursor and str(next_cursor) == str(cursor):
        return failed_page(
            provider=OPENALEX_PROVIDER,
            keyword_zh=keyword_zh,
            query_id=query_id,
            query=query,
            query_language=query_language,
            lane=lane,
            request_cursor=cursor,
            page_size=page_size,
            error_type="cursor_not_advancing",
            safe_error="OpenAlex next_cursor did not advance",
            failure_class="terminal",
        )
    candidates = [parse_openalex_work(work, query=query) for work in results]
    exhausted = (not results) or (not next_cursor)
    return DiscoveryPage(
        provider=OPENALEX_PROVIDER,
        keyword_zh=keyword_zh,
        query_id=query_id,
        query=query,
        query_language=query_language,
        lane=lane,
        candidates=candidates,
        request_cursor=cursor,
        next_cursor=next_cursor if next_cursor else None,
        page_size=page_size,
        returned_count=len(candidates),
        total_results=total_results,
        status="success",
        exhausted=exhausted,
        response_metadata={
            "http_status": outcome.status_code,
            "provider_request_id": next(
                (str(value) for key, value in outcome.headers.items()
                 if str(key).lower() in {"x-request-id", "x-amzn-requestid", "x-correlation-id"}),
                None,
            ),
            "retry_after_observed": outcome.retry_after_observed,
            "total_results": total_results,
            "next_cursor_present": bool(next_cursor),
            "response_fingerprint": hashlib.sha256(response_body).hexdigest()[:16],
            "observed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
