"""Crossref DOI and BibTeX verification."""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Literal

import requests
from loguru import logger

from src.discovery.models import PaperCandidate, normalize_doi, normalize_title
from src.discovery.provider_models import DiscoveryPage, classify_http_error, failed_page
from src.fetch.proxy import get_fetch_proxies


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


def resolve_doi_match_by_title(title: str, year: int | None = None, domain_id: str | None = None) -> ResolvedDoiMatch | None:
    best = resolve_doi_by_title(title, year=year, domain_id=domain_id)
    if best is None:
        return None
    return ResolvedDoiMatch(
        doi=best.doi,
        provider=CROSSREF_PROVIDER,
        confidence=float(best.confidence or 0.0),
        matched_title=best.title,
        raw_record=best.raw if isinstance(best.raw, dict) else {},
    )


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



# ── Cursor-paginated page (Refresh/Backfill lanes) ──────────────────


def search_crossref_page(
    query: str,
    *,
    keyword_zh: str,
    query_id: str = "",
    query_language: str = "",
    lane: Literal["refresh", "backfill"],
    page_size: int,
    cursor: str = "*",
    sort: str | None = None,
    order: str | None = None,
    from_date: str = "",
    to_date: str = "",
    rate_limiter: Any | None = None,
    limiter_lock: Any | None = None,
    request_observer: Any | None = None,
) -> DiscoveryPage:
    """Fetch one Crossref works page via deep-paging cursor.

    Crossref cursor pagination: first request uses ``cursor="*"`` and
    the response carries ``message.next-cursor`` for the next page. When
    the returned item count is below ``page_size`` (or ``next-cursor``
    is empty) the page is marked ``exhausted=True``.

    On HTTP failure the page has ``status="failed"`` and
    ``next_cursor=None`` so the backfill cursor is NOT advanced.
    """
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
    # ── Apply time window to actual request ─────────────────────────
    if from_date:
        params["from-pub-date"] = from_date
    if to_date:
        params["until-pub-date"] = to_date

    lock_ctx = limiter_lock if limiter_lock is not None else nullcontext()
    try:
        if rate_limiter is not None:
            with lock_ctx:
                rate_limiter.wait(CROSSREF_PROVIDER)
        response = requests.get(
            CROSSREF_WORKS_URL,
            params=params,
            timeout=20,
            proxies=get_fetch_proxies(),
        )
        if rate_limiter is not None:
            with lock_ctx:
                rate_limiter.record_response(
                    CROSSREF_PROVIDER,
                    dict(response.headers),
                    response.status_code,
                )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        safe_error = _safe_crossref_error(exc)
        logger.warning(
            "Crossref page failed for {!r} (cursor={!r}): {}",
            query, cursor, safe_error,
        )
        _error_type, failure_class, http_status, retry_after = classify_http_error(exc)
        return failed_page(
            provider=CROSSREF_PROVIDER,
            keyword_zh=keyword_zh,
            query_id=query_id,
            query=query,
            query_language=query_language,
            lane=lane,
            request_cursor=cursor,
            page_size=page_size,
            error_type=_error_type,
            safe_error=safe_error,
            failure_class=failure_class,
            http_status=http_status,
            retry_after_seconds=retry_after,
        )

    # Keep local evidence failures out of the provider failure path.
    if request_observer is not None:
        from src.discovery.provider_request_evidence import (
            ActualRequestEvidence, RequestEvidenceError, build_safe_signature,
            safe_response_hash,
        )
        evidence = ActualRequestEvidence(
            safe_signature=build_safe_signature(
                provider=CROSSREF_PROVIDER, query=query,
                sort=sort or "", order=order or "", page_size=page_size,
                lane=lane, pagination_schema_version="2.0",
                time_window={"from": from_date, "to": to_date},
            ),
            cursor_in=cursor,
            cursor_out=str(data.get("message", {}).get("next-cursor") or ""),
            response_hash=safe_response_hash(response.content),
            observation_count=len(data.get("message", {}).get("items", []) or []),
            response_bytes=response.content,
        )
        try:
            request_observer(evidence)
        except RequestEvidenceError:
            raise
        except Exception as exc:
            raise RequestEvidenceError(
                f"Crossref request evidence observer failed: {type(exc).__name__}: {exc}"
            ) from exc

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
    )
