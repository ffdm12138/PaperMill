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
from typing import Literal, Mapping

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
    # Exact response evidence supplied by ProviderPageFetcher production
    # adapters.  A page journal may persist only this sanitized metadata,
    # never raw URLs, headers, credentials, or response bodies.
    response_metadata: Mapping[str, object] | None = None

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
            "response_metadata": (
                None if self.response_metadata is None else dict(self.response_metadata)
            ),
        }


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
