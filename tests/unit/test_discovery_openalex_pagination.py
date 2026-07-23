"""Unit tests for OpenAlex cursor pagination (search_openalex_page).

All requests go through the unified ProviderClient; tests inject a
``FakeTransport``-backed client — no real network, no ``requests`` mocks.
"""
from __future__ import annotations

import threading

import pytest

from src.discovery.providers.provider_client import ProviderClient, ProviderTelemetry
from src.discovery.providers.provider_client import CircuitBreaker
from src.discovery.execution.lane_models import DiscoveryLaneKey, LaneExecutionSpec, RequestSignature
from src.discovery.search_openalex import search_openalex_page
from src.services.rate_limit import ProviderRateLimiter, default_config
from tests.helpers.fake_provider import FakeClock, FakeSleeper, FakeTransport, Fault, http_response


pytestmark = pytest.mark.unit


def _openalex_response(results, next_cursor, count=None):
    """Build a fake OpenAlex /works JSON response."""
    return {
        "meta": {"next_cursor": next_cursor, "count": count if count is not None else len(results)},
        "results": results,
    }


def _work(doi, title="T"):
    return {
        "id": f"https://openalex.org/W{doi}",
        "display_name": title,
        "doi": f"https://doi.org/{doi}",
        "publication_year": 2020,
        "authorships": [],
        "primary_location": {},
        "open_access": {"is_oa": False},
    }


def _client(script):
    cfg = default_config()
    cfg["global"]["paper_interval_seconds"] = 0.0
    cfg["global"]["jitter_seconds"] = 0.0
    cfg["providers"]["openalex"]["min_interval_seconds"] = 0.0
    transport = FakeTransport(list(script))
    clock = FakeClock()
    client = ProviderClient(
        "openalex",
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
        filters={"provider": "openalex", "mode": mode},
        page_size=page_size,
    )
    return LaneExecutionSpec(
        key=DiscoveryLaneKey(
            keyword_id="keyword-id",
            query_id="query-id",
            provider="openalex",
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


class TestOpenAlexPage:
    def test_success_with_next_cursor(self):
        data = _openalex_response([_work("10.1/a"), _work("10.1/b")], next_cursor="CURSOR2")
        client, _ = _client([http_response(200, data)])
        page = search_openalex_page(_spec(mode="backfill", page_size=25), "CURSOR1", client)
        assert page.status == "success"
        assert page.next_cursor == "CURSOR2"
        assert page.returned_count == 2
        assert page.exhausted is False
        assert page.request_cursor == "CURSOR1"

    def test_success_exhausted_when_no_next_cursor(self):
        data = _openalex_response([_work("10.1/a")], next_cursor=None)
        client, _ = _client([http_response(200, data)])
        page = search_openalex_page(_spec(mode="refresh", page_size=25), "*", client)
        assert page.status == "success"
        assert page.exhausted is True
        assert page.next_cursor is None

    def test_failure_does_not_return_cursor(self):
        client, _ = _client([http_response(500), http_response(500), http_response(500)])
        page = search_openalex_page(_spec(mode="backfill", page_size=25), "CURSOR1", client)
        assert page.status == "failed"
        assert page.next_cursor is None
        assert page.request_cursor == "CURSOR1"
        assert page.exhausted is False
        assert page.safe_error is not None

    def test_network_exception_returns_failed_page(self):
        class ConnectionError_(Exception):
            pass

        client, _ = _client([Fault(ConnectionError_("refused"))] * 3)
        page = search_openalex_page(_spec(mode="backfill", page_size=25), "CURSOR1", client)
        assert page.status == "failed"
        assert page.next_cursor is None

    def test_refresh_uses_star_cursor(self):
        data = _openalex_response([_work("10.1/a")], next_cursor="NEXT")
        client, transport = _client([http_response(200, data)])
        search_openalex_page(_spec(mode="refresh", page_size=25), "*", client)
        sent_params = transport.requests[0].params
        assert sent_params.get("cursor") == "*"
