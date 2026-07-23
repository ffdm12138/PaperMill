"""resolve_crossref 兼容接口测试：注入 FakeTransport 客户端，不访问真实网络。"""
from __future__ import annotations

import threading

from src.discovery.providers.provider_client import CircuitBreaker, ProviderClient, ProviderTelemetry
from src.discovery.resolve_crossref import (
    get_crossref_work_by_doi,
    resolve_crossref_by_title,
    resolve_doi_by_title,
)
from src.services.rate_limit import ProviderRateLimiter, default_config
from tests.helpers.fake_provider import FakeClock, FakeSleeper, FakeTransport, Fault, http_response


def _client(script):
    cfg = default_config()
    cfg["global"]["paper_interval_seconds"] = 0.0
    cfg["global"]["jitter_seconds"] = 0.0
    cfg["providers"]["crossref"]["min_interval_seconds"] = 0.0
    clock = FakeClock()
    return ProviderClient(
        "crossref",
        limiter=ProviderRateLimiter(cfg),
        limiter_lock=threading.Lock(),
        breaker=CircuitBreaker(failure_threshold=50, recovery_seconds=30.0),
        request_budget=None,
        sleeper=FakeSleeper(clock),
        clock=clock,
        transport=FakeTransport(list(script)),
        telemetry=ProviderTelemetry(),
        max_retries=1,
    )


def _works_payload(items):
    return {"status": "ok", "message": {"items": items}}


def test_resolve_crossref_by_title_ranks_by_similarity_and_year():
    items = [
        {"DOI": "10.1/a", "title": ["Blowing Snow Sublimation Study"], "issued": {"date-parts": [[2020]]}},
        {"DOI": "10.1/b", "title": ["Completely Unrelated Ocean Topic"], "issued": {"date-parts": [[2001]]}},
    ]
    client = _client([http_response(200, _works_payload(items))])
    ranked = resolve_crossref_by_title("blowing snow sublimation", year=2020, limit=5, client=client)
    assert len(ranked) == 2
    assert ranked[0].doi == "10.1/a"
    assert ranked[0].confidence >= 0.0
    assert ranked[0].confidence >= ranked[1].confidence


def test_resolve_crossref_by_title_returns_empty_on_network_error():
    class Boom(Exception):
        pass

    client = _client([Fault(Boom("network down")), Fault(Boom("network down"))])
    assert resolve_crossref_by_title("anything", client=client) == []


def test_get_crossref_work_by_doi_returns_message():
    payload = {"message": {"DOI": "10.1/x", "title": ["t"]}}
    client = _client([http_response(200, payload)])
    work = get_crossref_work_by_doi("https://doi.org/10.1/x", client=client)
    assert work == payload["message"]


def test_get_crossref_work_by_doi_returns_none_on_error():
    class Boom(Exception):
        pass

    client = _client([Fault(Boom("network down")), Fault(Boom("network down"))])
    assert get_crossref_work_by_doi("10.1/x", client=client) is None


def test_get_crossref_work_by_doi_rejects_empty_doi():
    assert get_crossref_work_by_doi("") is None


def test_resolve_doi_by_title_contract_preserved():
    items = [
        {"DOI": "10.1/a", "title": ["Blowing Snow Sublimation Study"], "issued": {"date-parts": [[2020]]}},
    ]
    client = _client([http_response(200, _works_payload(items))])
    best = resolve_doi_by_title("blowing snow sublimation study", year=2020, client=client)
    assert best is not None
    assert best.doi == "10.1/a"

    items_bad = [{"DOI": "10.1/b", "title": ["Unrelated Ocean"], "issued": {"date-parts": [[2001]]}}]
    client_bad = _client([http_response(200, _works_payload(items_bad))])
    assert resolve_doi_by_title("blowing snow sublimation study", year=2020, client=client_bad) is None
