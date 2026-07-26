"""Unit tests for Crossref cursor pagination (search_crossref_page).

All requests go through the unified ProviderClient; tests inject a
``FakeTransport``-backed client — no real network, no ``requests`` mocks.
"""
from __future__ import annotations

import threading

import pytest

from src.discovery.providers.provider_client import CircuitBreaker, ProviderClient, ProviderTelemetry
from src.discovery.execution.lane_models import DiscoveryLaneKey, LaneExecutionSpec, RequestSignature
from src.discovery.resolve_crossref import search_crossref_page
from src.utils.rate_limit import ProviderRateLimiter, default_config
from tests.helpers.fake_provider import FakeClock, FakeSleeper, FakeTransport, Fault, http_response


pytestmark = pytest.mark.unit


def _crossref_response(items, next_cursor, total=None):
    return {
        "status": "ok",
        "message": {
            "items": items,
            "total-results": total if total is not None else len(items),
            **({"next-cursor": next_cursor} if next_cursor is not None else {}),
        },
    }


def _item(doi, title="T"):
    return {
        "DOI": doi,
        "title": [title],
        "container-title": ["Journal"],
        "author": [],
        "URL": f"https://doi.org/{doi}",
        "is-referenced-by-count": 0,
    }


def _client(script):
    cfg = default_config()
    cfg["global"]["paper_interval_seconds"] = 0.0
    cfg["global"]["jitter_seconds"] = 0.0
    cfg["providers"]["crossref"]["min_interval_seconds"] = 0.0
    transport = FakeTransport(list(script))
    clock = FakeClock()
    client = ProviderClient(
        "crossref",
        limiter=ProviderRateLimiter(cfg),
        limiter_lock=threading.Lock(),
        breaker=CircuitBreaker(failure_threshold=50, recovery_seconds=30.0),
        request_budget=None,
        sleeper=FakeSleeper(clock),
        clock=clock,
        transport=transport,
        telemetry=ProviderTelemetry(),
        max_retries=2,
    )
    return client, transport


def _spec(*, mode: str, page_size: int) -> LaneExecutionSpec:
    signature = RequestSignature.create(
        sort=None,
        filters={"provider": "crossref", "mode": mode},
        page_size=page_size,
    )
    return LaneExecutionSpec(
        key=DiscoveryLaneKey(
            keyword_id="keyword-id",
            query_id="query-id",
            provider="crossref",
            mode=mode,  # type: ignore[arg-type]
            generation=1,
            request_signature=signature.hash,
        ),
        request_signature=signature,
        keyword_zh="边界层",
        query="boundary layer",
        query_language="en",
        relevance_profile_hash="profile-hash",
        refresh_run_id="refresh-run" if mode == "refresh" else None,
    )


class TestCrossrefPage:
    def test_success_with_next_cursor(self):
        data = _crossref_response([_item("10.1/a"), _item("10.1/b")], next_cursor="CR2")
        client, _ = _client([http_response(200, data)])
        page = search_crossref_page(_spec(mode="backfill", page_size=50), "CR1", client)
        assert page.status == "success"
        assert page.next_cursor == "CR2"
        assert page.returned_count == 2
        assert page.exhausted is False

    def test_exhausted_when_no_next_cursor(self):
        data = _crossref_response([_item("10.1/a")], next_cursor=None)
        client, _ = _client([http_response(200, data)])
        page = search_crossref_page(_spec(mode="backfill", page_size=50), "CR1", client)
        assert page.exhausted is True

    def test_short_page_with_next_cursor_not_exhausted(self):
        """A short page that still carries next-cursor must NOT be marked exhausted."""
        data = _crossref_response([_item("10.1/a")], next_cursor="CR2")
        client, _ = _client([http_response(200, data)])
        page = search_crossref_page(_spec(mode="backfill", page_size=50), "CR1", client)
        assert page.exhausted is False

    def test_failure_does_not_advance_cursor(self):
        client, _ = _client([http_response(503), http_response(503), http_response(503)])
        page = search_crossref_page(_spec(mode="backfill", page_size=50), "CR1", client)
        assert page.status == "failed"
        assert page.next_cursor is None
        assert page.request_cursor == "CR1"

    def test_safe_error_strips_url_query(self):
        """Error sanitization must not leak URL params (which may carry keys)."""
        class ConnectionError_(Exception):
            pass

        client, _ = _client(
            [Fault(ConnectionError_("GET https://api.crossref.org/works?secret=ABCDEF timeout"))] * 3
        )
        page = search_crossref_page(_spec(mode="backfill", page_size=50), "CR1", client)
        assert page.status == "failed"
        assert "ABCDEF" not in (page.safe_error or "")
