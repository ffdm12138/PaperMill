"""Contract tests for the batch-level TitleResolutionService.

Covers: batch-level budget (shared, not per-drain), in-batch dedup,
durable cache, 429 dispatch freeze, and shared limiter (invariant #9).
"""
from __future__ import annotations

import threading

import pytest

from src.discovery.runtime.budgets import BatchDoiResolutionBudget
from src.discovery.providers.provider_client import CircuitBreaker, ProviderClient, ProviderTelemetry
from src.discovery.title_resolution import DurableTitleCache, TitleResolutionService
from src.services.rate_limit import ProviderRateLimiter, default_config
from tests.helpers.fake_provider import FakeClock, FakeSleeper, FakeTransport, http_response


def _client(script, provider="crossref"):
    cfg = default_config()
    cfg["global"]["paper_interval_seconds"] = 0.0
    cfg["global"]["jitter_seconds"] = 0.0
    cfg["providers"][provider]["min_interval_seconds"] = 0.0
    clock = FakeClock()
    transport = FakeTransport(list(script))
    client = ProviderClient(
        provider,
        limiter=ProviderRateLimiter(cfg),
        limiter_lock=threading.Lock(),
        breaker=CircuitBreaker(failure_threshold=50, recovery_seconds=30.0),
        request_budget=None,
        sleeper=FakeSleeper(clock),
        clock=clock,
        transport=transport,
        telemetry=ProviderTelemetry(),
        max_retries=1,
    )
    return client, transport


def _crossref_hit(doi: str, title: str):
    return http_response(200, {
        "status": "ok",
        "message": {"items": [{
            "DOI": doi,
            "title": [title],
            "container-title": ["J"],
            "issued": {"date-parts": [[2020]]},
        }]},
    })


def test_batch_budget_shared_across_services(tmp_path):
    """Two drains (two services sharing one budget) cannot exceed the batch limit."""
    budget = BatchDoiResolutionBudget(limit=1)
    client, transport = _client([_crossref_hit("10.1/a", "Alpha Study")])
    s1 = TitleResolutionService(client=client, budget=budget, cache=DurableTitleCache(None))
    s2 = TitleResolutionService(client=client, budget=budget, cache=DurableTitleCache(None))
    assert s1.resolve("alpha study", year=2020) is not None
    # Batch budget now exhausted; second drain's different title gets nothing.
    assert s2.resolve("bravo study", year=2020) is None
    assert transport.request_count == 1
    assert budget.snapshot()["attempted"] == 1


def test_in_batch_dedup_resolves_title_once(tmp_path):
    budget = BatchDoiResolutionBudget(limit=10)
    client, transport = _client([_crossref_hit("10.1/a", "Alpha Study")])
    service = TitleResolutionService(client=client, budget=budget, cache=DurableTitleCache(None))
    first = service.resolve("alpha study", year=2020)
    second = service.resolve("alpha study", year=2020)
    assert first is not None and second is not None
    assert first.doi == second.doi == "10.1/a"
    assert transport.request_count == 1
    snap = budget.snapshot()
    assert snap["dedup_hits"] == 1
    assert snap["attempted"] == 1


def test_durable_cache_survives_restart(tmp_path):
    cache_dir = tmp_path / "title_cache"
    budget = BatchDoiResolutionBudget(limit=10)
    client, transport = _client([_crossref_hit("10.1/a", "Alpha Study")])
    s1 = TitleResolutionService(client=client, budget=budget, cache=DurableTitleCache(cache_dir))
    assert s1.resolve("alpha study", year=2020) is not None
    assert transport.request_count == 1

    # "Restart": brand-new service + client with an empty script; the durable
    # cache must answer without any HTTP.
    client2, transport2 = _client([], provider="crossref")
    budget2 = BatchDoiResolutionBudget(limit=10)
    s2 = TitleResolutionService(client=client2, budget=budget2, cache=DurableTitleCache(cache_dir))
    match = s2.resolve("alpha study", year=2020)
    assert match is not None and match.doi == "10.1/a"
    assert transport2.request_count == 0
    assert budget2.snapshot()["cache_hits"] == 1


def test_429_freezes_dispatch_for_batch(tmp_path):
    budget = BatchDoiResolutionBudget(limit=10)
    client, transport = _client([http_response(429), http_response(429)])
    service = TitleResolutionService(client=client, budget=budget, cache=DurableTitleCache(None))
    assert service.resolve("alpha study", year=2020) is None
    assert budget.snapshot()["stopped_by_rate_limit"] is True
    # Subsequent resolves (even different titles) never hit the wire.
    assert service.resolve("bravo study", year=2021) is None
    assert transport.request_count == 2  # 1 initial + 1 retry, then frozen


def test_shared_limiter_with_discovery_pages(tmp_path):
    """Invariant #9: title resolution and discovery pages share the provider limiter."""
    cfg = default_config()
    cfg["global"]["paper_interval_seconds"] = 0.0
    cfg["global"]["jitter_seconds"] = 0.0
    cfg["providers"]["crossref"]["min_interval_seconds"] = 0.0
    limiter = ProviderRateLimiter(cfg)
    clock = FakeClock()
    telemetry = ProviderTelemetry()
    transport = FakeTransport([
        _crossref_hit("10.1/a", "Alpha Study"),
        http_response(200, {"status": "ok", "message": {"items": [], "total-results": 0}}),
    ])

    def make_client():
        return ProviderClient(
            "crossref",
            limiter=limiter,
            limiter_lock=threading.Lock(),
            breaker=CircuitBreaker(failure_threshold=50, recovery_seconds=30.0),
            request_budget=None,
            sleeper=FakeSleeper(clock),
            clock=clock,
            transport=transport,
            telemetry=telemetry,
            max_retries=0,
        )

    budget = BatchDoiResolutionBudget(limit=10)
    service = TitleResolutionService(client=make_client(), budget=budget, cache=DurableTitleCache(None))
    service.resolve("alpha study", year=2020)

    # A discovery_page request through a second client on the SAME limiter.
    from src.discovery.providers.provider_client import RequestSpec

    page_client = make_client()
    outcome = page_client.execute(RequestSpec(
        provider="crossref", purpose="discovery_page",
        url="https://api.crossref.org/works",
        params={"query.bibliographic": "x", "rows": 1, "cursor": "*"},
    ))
    assert outcome.status_code == 200
    snap = telemetry.snapshot()
    assert snap["crossref.title_resolution.attempted"] == 1
    assert snap["crossref.discovery_page.attempted"] == 1
    # Both purposes incremented the SAME limiter's request count.
    assert limiter.stats.requests_by_provider.get("crossref") == 2
