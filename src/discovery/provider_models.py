"""Provider page-result model for dual-lane DOI discovery.

A ``DiscoveryPage`` is the unit returned by a single provider page request
(OpenAlex or Crossref). It distinguishes network failures from empty
results so that Backfill cursors are only advanced on genuine success.

Design rules (see ``docs/PROJECT_CONTRACT.md`` keyword notebook section):

- ``status="failed"`` pages must NOT advance the backfill cursor.
- ``status="success"`` with zero results may mark ``exhausted=True``.
- Error fields must never leak credentials, cookies, authorization
  headers, TDM tokens, API keys, or proxy URLs — use ``safe_error``.
- ``request_cursor`` is the cursor used for THIS page; ``next_cursor``
  is what the provider returned for the NEXT page (``None`` if exhausted
  or failed).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from src.discovery.models import PaperCandidate


Lane = Literal["refresh", "backfill"]
PageStatus = Literal["success", "failed"]
FailureClass = Literal["retryable", "terminal", "transient"]
QueryLanguage = Literal["zh", "en", "mixed"]


@dataclass(frozen=True)
class ProviderSearchRequest:
    """Canonical identity passed to a provider lane."""

    keyword_id: str
    keyword_zh: str
    query_id: str
    query: str
    query_language: QueryLanguage
    provider: str
    lane: Lane


@dataclass(frozen=True)
class DiscoveryPage:
    """One page of results from a single provider request."""

    provider: str
    keyword_zh: str
    query: str
    lane: Lane
    query_id: str = ""
    query_language: QueryLanguage | str = ""
    candidates: list[PaperCandidate] = field(default_factory=list)
    request_cursor: str | None = None
    next_cursor: str | None = None
    page_size: int = 0
    returned_count: int = 0
    total_results: int | None = None
    status: PageStatus = "success"
    exhausted: bool = False
    error_type: str | None = None
    safe_error: str | None = None
    # Structured failure classification (Phase 3).
    failure_class: FailureClass | None = None
    http_status: int | None = None
    retry_after_seconds: float | None = None

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "keyword_zh": self.keyword_zh,
            "query_id": self.query_id,
            "query": self.query,
            "query_language": self.query_language,
            "lane": self.lane,
            "candidates": [c.to_dict() for c in self.candidates],
            "request_cursor": self.request_cursor,
            "next_cursor": self.next_cursor,
            "page_size": self.page_size,
            "returned_count": self.returned_count,
            "total_results": self.total_results,
            "status": self.status,
            "exhausted": self.exhausted,
            "error_type": self.error_type,
            "safe_error": self.safe_error,
            "failure_class": self.failure_class,
            "http_status": self.http_status,
            "retry_after_seconds": self.retry_after_seconds,
        }


def classify_http_error(
    exc: Exception,
) -> tuple[str, FailureClass, int | None, float | None]:
    """Classify a requests exception into (error_type, failure_class, http_status, retry_after).

    Classification rules:
    - 400, 401, 403 (no rate-limit evidence), 404, 410, 422: **terminal**
    - 403 with Retry-After header: **retryable** (rate-limited)
    - 408, 425, 429, 500, 502, 503, 504: **retryable**
    - ConnectionError / Timeout: **transient** (no http_status)
    - Unknown: **retryable** (safe default)

    The returned ``error_type`` is the exception class name.  The
    ``retry_after_seconds`` is parsed from the ``Retry-After`` header
    when present.
    """
    try:
        import requests
    except ImportError:  # pragma: no cover — requests is a hard dependency
        requests = None  # type: ignore[assignment]

    if requests is not None and isinstance(exc, requests.HTTPError):
        response = getattr(exc, "response", None)
        status = response.status_code if response is not None else None
        if status is not None:
            # Parse Retry-After header (if present) for rate-limit signals.
            retry_after: float | None = None
            if response is not None:
                ra = response.headers.get("Retry-After")
                if ra:
                    try:
                        retry_after = float(ra)
                    except (ValueError, TypeError):
                        pass
            # Terminal HTTP statuses (won't succeed on retry).
            if status in (400, 401, 404, 410, 422):
                return type(exc).__name__, "terminal", status, None
            # 403: terminal unless there is rate-limit evidence.
            if status == 403:
                if retry_after is not None:
                    return type(exc).__name__, "retryable", status, retry_after
                return type(exc).__name__, "terminal", status, None
            # Retryable HTTP statuses.
            if status in (408, 425, 429, 500, 502, 503, 504):
                return type(exc).__name__, "retryable", status, retry_after
            # Unknown HTTP status — default to retryable.
            return type(exc).__name__, "retryable", status, retry_after
    if requests is not None and isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return type(exc).__name__, "transient", None, None
    # Unknown exception — default to retryable.
    return type(exc).__name__, "retryable", None, None


def failed_page(
    *,
    provider: str,
    keyword_zh: str,
    query: str,
    lane: Lane,
    request_cursor: str | None,
    page_size: int,
    error_type: str,
    safe_error: str,
    query_id: str = "",
    query_language: QueryLanguage | str = "",
    failure_class: FailureClass | None = None,
    http_status: int | None = None,
    retry_after_seconds: float | None = None,
) -> DiscoveryPage:
    """Build a failed page that does NOT advance the cursor."""
    return DiscoveryPage(
        provider=provider,
        keyword_zh=keyword_zh,
        query_id=query_id,
        query=query,
        query_language=query_language,
        lane=lane,
        candidates=[],
        request_cursor=request_cursor,
        next_cursor=None,
        page_size=page_size,
        returned_count=0,
        total_results=None,
        status="failed",
        exhausted=False,
        error_type=error_type,
        safe_error=safe_error,
        failure_class=failure_class,
        http_status=http_status,
        retry_after_seconds=retry_after_seconds,
    )
