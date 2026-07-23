"""Fake provider transport/clock/sleeper harness for discovery tests.

All discovery provider tests run against these fakes — zero real network,
zero real sleeping, deterministic ordering.
"""
from __future__ import annotations

import json
import hashlib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from src.discovery.providers.provider_client import RawResponse, RequestSpec
from src.discovery.providers.provider_models import DiscoveryPage


@dataclass(frozen=True)
class Fault:
    """A scripted transport fault (raised instead of returning a response)."""

    exc: Exception


def http_response(
    status: int = 200,
    body: Mapping[str, Any] | list | None = None,
    headers: Mapping[str, str] | None = None,
) -> RawResponse:
    payload = b"" if body is None else json.dumps(body).encode("utf-8")
    return RawResponse(status_code=status, headers=dict(headers or {}), body=payload)


class FakeTransport:
    """Scripted transport: pops one scripted item per ``send`` call.

    Each script item is either a :class:`RawResponse` or a :class:`Fault`.
    When the script is exhausted the transport raises ``AssertionError``
    (tests must script exactly the requests they expect).
    """

    def __init__(self, script: list[RawResponse | Fault] | None = None) -> None:
        self._script = list(script or [])
        self.requests: list[RequestSpec] = []
        self._lock = threading.Lock()

    def send(self, spec: RequestSpec, timeout_seconds: float) -> RawResponse:
        with self._lock:
            self.requests.append(spec)
            if not self._script:
                raise AssertionError(
                    f"FakeTransport script exhausted at {spec.url} ({spec.purpose})"
                )
            item = self._script.pop(0)
        if isinstance(item, Fault):
            raise item.exc
        return item

    @property
    def request_count(self) -> int:
        return len(self.requests)


class FakeClock:
    """Deterministic monotonic clock."""

    def __init__(self, start: float = 1_000.0) -> None:
        self._now = float(start)
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += float(seconds)


class FakeSleeper:
    """Records sleep durations instead of actually sleeping."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.sleeps: list[float] = []
        self._clock = clock
        self._lock = threading.Lock()

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(float(seconds))
        if self._clock is not None:
            self._clock.advance(seconds)


def make_openalex_page(
    items: list[Mapping[str, Any]],
    next_cursor: str | None = None,
) -> RawResponse:
    """Build a realistic OpenAlex /works cursor-page response."""
    return http_response(
        200,
        {
            "meta": {"count": len(items), "next_cursor": next_cursor},
            "results": list(items),
        },
    )


def make_crossref_page(
    items: list[Mapping[str, Any]],
    next_cursor: str | None = None,
) -> RawResponse:
    """Build a realistic Crossref /works cursor-page response."""
    message: dict[str, Any] = {"items": list(items), "total-results": len(items)}
    if next_cursor is not None:
        message["next-cursor"] = next_cursor
    return http_response(200, {"status": "ok", "message": message})


def provider_response_metadata(
    *,
    provider: str,
    cursor: str,
    next_cursor: str | None,
    returned_count: int,
    http_status: int = 200,
    total_results: int | None = None,
    request_id: str | None = None,
) -> dict[str, object]:
    """Return complete sanitized response evidence for a fake page.

    Tests deliberately use the same v3 durable-page contract as production:
    a fake success cannot omit a real status, fingerprint, timestamp, or
    next-cursor fact.  The fingerprint identifies only synthetic fixture
    facts and contains no URL/query/credential material.
    """
    payload = json.dumps(
        {
            "provider": provider,
            "cursor": cursor,
            "next_cursor": next_cursor,
            "returned_count": returned_count,
            "http_status": http_status,
            "total_results": total_results,
            "request_id": request_id,
        },
        sort_keys=True,
    ).encode("utf-8")
    return {
        "http_status": http_status,
        "provider_request_id": request_id,
        "retry_after_observed": None,
        "total_results": total_results,
        "next_cursor_present": next_cursor is not None,
        "response_fingerprint": hashlib.sha256(payload).hexdigest(),
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def discovery_page(
    *,
    provider: str,
    keyword_zh: str,
    query: str,
    lane: str,
    cursor: str,
    candidates: list[Any] | None = None,
    query_id: str = "",
    query_language: str = "",
    next_cursor: str | None = None,
    exhausted: bool = False,
    status: str = "success",
    safe_error: str | None = None,
    error_type: str | None = None,
    failure_class: str | None = None,
    http_status: int = 200,
    total_results: int | None = None,
) -> DiscoveryPage:
    """Construct an actual typed ``DiscoveryPage`` for a fake fetcher."""
    materialized = list(candidates or [])
    return DiscoveryPage(
        provider=provider,
        keyword_zh=keyword_zh,
        query=query,
        lane=lane,  # type: ignore[arg-type]
        query_id=query_id,
        query_language=query_language,
        candidates=materialized,
        request_cursor=cursor,
        next_cursor=next_cursor,
        page_size=len(materialized),
        returned_count=len(materialized),
        total_results=total_results,
        status=status,  # type: ignore[arg-type]
        exhausted=exhausted,
        safe_error=safe_error,
        error_type=error_type,
        failure_class=failure_class,  # type: ignore[arg-type]
        http_status=http_status,
        response_metadata=(
            provider_response_metadata(
                provider=provider,
                cursor=cursor,
                next_cursor=next_cursor,
                returned_count=len(materialized),
                http_status=http_status,
                total_results=total_results,
            )
            if status == "success" else None
        ),
    )


# ── unified test factory for DiscoveryLaneKey ──────────────────────────

def lane_key_for_test(
    *,
    keyword_id: str = "a1b2c3d4e5f6a7b8",
    query_id: str = "q1",
    provider: str = "openalex",
    mode: str = "backfill",
    generation: int = 1,
    sort: str = "published",
    filters: dict[str, object] | None = None,
    page_size: int = 25,
    pagination_schema_version: str = "2.0",
) -> "DiscoveryLaneKey":
    """Create a DiscoveryLaneKey with a real RequestSignature hash.

    Every test that constructs a DiscoveryLaneKey must use this factory
    (or an equivalent path through RequestSignature.create()) so that
    the ``request_signature`` field is always a valid hash — never an
    empty string, a fake placeholder, or a bare mode string.

    Defaults produce a repeatable backfill lane key suitable for most
    unit tests.  Override keyword_id / query_id / provider / mode /
    generation as needed for the scenario under test.
    """
    from src.discovery.execution.lane_models import DiscoveryLaneKey, RequestSignature

    sig = RequestSignature.create(
        sort=sort,
        filters=filters or {},
        page_size=page_size,
        pagination_schema_version=pagination_schema_version,
    )
    return DiscoveryLaneKey(
        keyword_id=keyword_id,
        query_id=query_id,
        provider=provider,
        mode=mode,
        generation=generation,
        request_signature=sig.hash,
    )
