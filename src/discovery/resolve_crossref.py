"""Crossref DOI and bibliographic search.

All HTTP goes through the unified :class:`~src.discovery.provider_client.ProviderClient`
(limiter + retry + backoff + circuit breaker + telemetry).  This module only
builds request specs and parses responses — it never imports ``requests``.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

from loguru import logger

from src.discovery.models import PaperCandidate, normalize_doi, normalize_title
from src.discovery.execution.lane_models import LaneExecutionSpec
from src.discovery.providers.provider_client import ProviderClient, ProviderRuntime, RequestSpec
from src.discovery.providers.provider_errors import (
    ProviderError,
    ProviderProtocolError,
    ProviderRateLimited,
    ProviderRequestBudgetExhausted,
)
from src.discovery.providers.provider_models import DiscoveryPage, failed_page


CROSSREF_WORKS_URL = "https://api.crossref.org/works"
CROSSREF_PROVIDER = "crossref"


@dataclass(frozen=True)
class ResolvedDoiMatch:
    doi: str
    provider: str
    confidence: float
    matched_title: str
    raw_record: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "doi": normalize_doi(self.doi),
            "provider": self.provider,
            "confidence": self.confidence,
            "matched_title": self.matched_title,
            "raw_record": self.raw_record,
        }


def _safe_crossref_error(exc: Exception) -> str:
    """Sanitize a Crossref error for reports (no URL params / keys)."""
    msg = type(exc).__name__
    detail = str(exc)
    # Truncate and strip anything that looks like a URL query string.
    if "?" in detail:
        detail = detail.split("?", 1)[0]
    return f"{msg}: {detail[:200]}" if detail else msg


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
        abstract=str(item.get("abstract") or ""),
        source="crossref",
        source_id=item.get("DOI") or "",
        url=item.get("URL") or "",
        citation_count=item.get("is-referenced-by-count"),
        query=query,
        domain_id=domain_id,
        raw=item,
    )


def _runtime_client(client: ProviderClient | None) -> ProviderClient:
    """Return the injected client or the process-wide shared one."""
    if client is not None:
        return client
    return ProviderRuntime.get().client(CROSSREF_PROVIDER)


def search_crossref(
    query: str,
    domain_id: str | None = None,
    limit: int = 5,
    *,
    client: ProviderClient | None = None,
) -> list[PaperCandidate]:
    spec = RequestSpec(
        provider=CROSSREF_PROVIDER,
        purpose="title_resolution",
        url=CROSSREF_WORKS_URL,
        params={"query.bibliographic": query, "rows": limit},
        timeout_seconds=20,
    )
    try:
        outcome = _runtime_client(client).execute(spec)
        data = outcome.json()
    except ProviderRateLimited:
        # Re-raise so the batch-level TitleResolutionService can freeze
        # dispatch for the rest of the batch (429 must not be swallowed
        # into an empty result, otherwise every worker keeps hammering).
        raise
    except ProviderRequestBudgetExhausted:
        # The executor must map the shared request valve directly to a clean
        # BUDGET_STOPPED outcome instead of receiving a failed provider page.
        raise
    except ProviderError as exc:
        logger.warning("Crossref search failed for {!r}: {}", query, type(exc).__name__)
        return []
    items = (data.get("message") or {}).get("items") or []
    return [parse_crossref_item(item, query=query, domain_id=domain_id) for item in items]


def resolve_crossref_by_title(
    title: str,
    year: int | None = None,
    limit: int = 5,
    domain_id: str | None = None,
    *,
    client: ProviderClient | None = None,
) -> list[PaperCandidate]:
    """按标题相似度 + 年份接近度对 Crossref 候选排序，返回完整候选列表。

    网络错误时返回空列表（由 ``search_crossref`` 吞咽）。不做阈值过滤——
    阈值过滤由 ``resolve_doi_by_title`` 负责。
    """
    candidates = search_crossref(title, domain_id=domain_id, limit=limit, client=client)
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


def resolve_doi_by_title(
    title: str,
    year: int | None = None,
    domain_id: str | None = None,
    *,
    client: ProviderClient | None = None,
) -> PaperCandidate | None:
    candidates = resolve_crossref_by_title(title, year=year, limit=5, domain_id=domain_id, client=client)
    if not candidates:
        return None
    best = candidates[0]
    if best.confidence >= 0.75:
        return best
    return None


def resolve_doi_match_by_title(
    title: str,
    year: int | None = None,
    domain_id: str | None = None,
    *,
    client: ProviderClient | None = None,
) -> ResolvedDoiMatch | None:
    best = resolve_doi_by_title(title, year=year, domain_id=domain_id, client=client)
    if best is None:
        return None
    return ResolvedDoiMatch(
        doi=best.doi,
        provider=CROSSREF_PROVIDER,
        confidence=float(best.confidence or 0.0),
        matched_title=best.title,
        raw_record=best.raw if isinstance(best.raw, dict) else {},
    )


def get_crossref_work_by_doi(doi: str, *, client: ProviderClient | None = None) -> dict | None:
    """按 DOI 取 Crossref work 的 message dict，网络错误返回 None。"""
    doi = normalize_doi(doi)
    if not doi:
        return None
    spec = RequestSpec(
        provider=CROSSREF_PROVIDER,
        purpose="metadata_resolution",
        url=f"{CROSSREF_WORKS_URL}/{doi}",
        timeout_seconds=20,
    )
    try:
        outcome = _runtime_client(client).execute(spec)
        data = outcome.json()
    except ProviderError as exc:
        logger.warning("Crossref work lookup failed for {!r}: {}", doi, type(exc).__name__)
        return None
    return data.get("message") if isinstance(data, dict) else None



# ── Cursor-paginated page (Refresh/Backfill lanes) ──────────────────


def search_crossref_page(
    lane_spec: LaneExecutionSpec,
    cursor: str,
    client: ProviderClient,
) -> DiscoveryPage:
    """Fetch one Crossref works page via deep-paging cursor.

    The immutable execution spec is the sole request identity.  Crossref
    cursor pagination: first request uses ``cursor="*"`` and
    the response carries ``message.next-cursor`` for the next page. When
    the returned item count is below ``page_size`` (or ``next-cursor``
    is empty) the page is marked ``exhausted=True``.

    On HTTP failure the page has ``status="failed"`` and
    ``next_cursor=None`` so the backfill cursor is NOT advanced.
    """
    if lane_spec.key.provider != CROSSREF_PROVIDER:
        raise ValueError("search_crossref_page requires a Crossref LaneExecutionSpec")
    query = lane_spec.query
    keyword_zh = lane_spec.keyword_zh
    query_id = lane_spec.key.query_id
    query_language = lane_spec.query_language
    lane = lane_spec.key.mode
    stable_lane_id = lane_spec.key.stable_id()
    page_size = lane_spec.page_size
    sort = lane_spec.sort or None
    order = lane_spec.order
    params: dict[str, str | int] = {
        "query.bibliographic": query,
        "rows": page_size,
        "cursor": cursor,
    }
    if sort:
        if sort not in {"relevance", "published", "cited"}:
            raise ValueError(f"invalid Crossref sort: {sort!r}")
        params["sort"] = sort
    if order:
        if order not in {"asc", "desc"}:
            raise ValueError(f"invalid Crossref order: {order!r}")
        params["order"] = order
    spec = RequestSpec(
        provider=CROSSREF_PROVIDER,
        purpose="discovery_page",
        url=CROSSREF_WORKS_URL,
        params=params,
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
        logger.warning("Crossref page protocol error for {!r}: {}", query, exc)
        return failed_page(
            provider=CROSSREF_PROVIDER,
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
        safe_error = _safe_crossref_error(exc)
        logger.warning(
            "Crossref page failed for {!r} (cursor={!r}): {}",
            query, cursor, safe_error,
        )
        failure_class = "retryable" if exc.retryable else "terminal"
        http_status = getattr(exc, "http_status", None)
        retry_after = getattr(exc, "retry_after_seconds", None)
        return failed_page(
            provider=CROSSREF_PROVIDER,
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

    message = data.get("message") or {}
    items = message.get("items") or []
    next_cursor = message.get("next-cursor")
    total_results = message.get("total-results")
    if next_cursor and str(next_cursor) == str(cursor):
        return failed_page(
            provider=CROSSREF_PROVIDER,
            keyword_zh=keyword_zh,
            query_id=query_id,
            query=query,
            query_language=query_language,
            lane=lane,
            request_cursor=cursor,
            page_size=page_size,
            error_type="cursor_not_advancing",
            safe_error="Crossref next-cursor did not advance",
            failure_class="terminal",
        )
    candidates = [parse_crossref_item(item, query=query) for item in items]
    # Crossref signals exhaustion by omitting next-cursor (or returning
    # an empty page). A short page with a next-cursor is NOT exhaustion —
    # Crossref can return fewer items than requested mid-stream.
    exhausted = (not next_cursor) or len(items) == 0
    return DiscoveryPage(
        provider=CROSSREF_PROVIDER,
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
