"""Contract tests for the unified ProviderClient.

Covers: error classification, retry/backoff, Retry-After, circuit breaker
open/half-open/recover, limiter sharing across purposes, request budget
counting (failures included), telemetry, and protocol errors.
"""
from __future__ import annotations

import threading

import pytest

from src.discovery.runtime.budgets import ProviderRequestBudget
from src.discovery.providers.provider_client import (
    CircuitBreaker,
    ProviderClient,
    ProviderRuntime,
    ProviderTelemetry,
    RequestSpec,
)
from src.discovery.providers.provider_errors import (
    CircuitOpenError,
    ProviderAuthError,
    ProviderCancelledError,
    ProviderConnectionError,
    ProviderPermanentError,
    ProviderProtocolError,
    ProviderRateLimited,
    ProviderRequestBudgetExhausted,
    ProviderTimeoutError,
    ProviderTransientError,
)
from tests.helpers.fake_provider import (
    FakeClock,
    FakeSleeper,
    FakeTransport,
    Fault,
    http_response,
)


def _client(
    *,
    script=None,
    budget: ProviderRequestBudget | None = None,
    max_retries: int = 3,
    breaker: CircuitBreaker | None = None,
    clock: FakeClock | None = None,
    sleeper: FakeSleeper | None = None,
    telemetry: ProviderTelemetry | None = None,
    provider: str = "openalex",
):
    from src.utils.rate_limit import ProviderRateLimiter, default_config

    clock = clock or FakeClock()
    sleeper = sleeper or FakeSleeper(clock)
    transport = FakeTransport(list(script or []))
    telemetry = telemetry or ProviderTelemetry()
    cfg = default_config()
    # Neutralize real sleeping inside the limiter for tests.
    cfg["global"]["paper_interval_seconds"] = 0.0
    cfg["global"]["jitter_seconds"] = 0.0
    cfg["providers"]["openalex"]["min_interval_seconds"] = 0.0
    cfg["providers"]["crossref"]["min_interval_seconds"] = 0.0
    limiter = ProviderRateLimiter(cfg)
    client = ProviderClient(
        provider,
        limiter=limiter,
        limiter_lock=threading.Lock(),
        breaker=breaker or CircuitBreaker(failure_threshold=3, recovery_seconds=30.0),
        request_budget=budget,
        sleeper=sleeper,
        clock=clock,
        transport=transport,
        telemetry=telemetry,
        max_retries=max_retries,
        backoff_initial_seconds=1.0,
        backoff_multiplier=2.0,
        backoff_max_seconds=60.0,
    )
    return client, transport, sleeper, clock, telemetry


def _spec(purpose: str = "discovery_page") -> RequestSpec:
    return RequestSpec(provider="openalex", purpose=purpose, url="https://api.openalex.org/works")


def test_success_first_try() -> None:
    client, transport, sleeper, clock, telemetry = _client(
        script=[http_response(200, {"ok": True})]
    )
    outcome = client.execute(_spec())
    assert outcome.status_code == 200
    assert outcome.attempts == 1
    assert outcome.retries == 0
    assert transport.request_count == 1
    snap = telemetry.snapshot()
    assert snap["openalex.discovery_page.attempted"] == 1
    assert snap["openalex.discovery_page.succeeded"] == 1


def test_429_honors_retry_after_then_succeeds() -> None:
    client, transport, sleeper, clock, telemetry = _client(
        script=[
            http_response(429, headers={"Retry-After": "17"}),
            http_response(200, {"ok": True}),
        ]
    )
    outcome = client.execute(_spec())
    assert outcome.status_code == 200
    assert outcome.attempts == 2
    assert outcome.retry_after_observed == 17.0
    assert sleeper.sleeps == [17.0]  # Retry-After takes precedence over backoff


def test_429_exhausts_retries_raises_rate_limited() -> None:
    # Breaker threshold above the retry count so retry exhaustion (not the
    # breaker) is what terminates this lane.
    client, transport, sleeper, clock, telemetry = _client(
        script=[http_response(429), http_response(429), http_response(429), http_response(429)],
        max_retries=3,
        breaker=CircuitBreaker(failure_threshold=10, recovery_seconds=30.0),
    )
    with pytest.raises(ProviderRateLimited):
        client.execute(_spec())
    assert transport.request_count == 4  # 1 + 3 retries, all counted
    snap = telemetry.snapshot()
    assert snap["openalex.discovery_page.attempted"] == 4
    assert snap["openalex.discovery_page.failed"] == 1
    assert snap["openalex.discovery_page.retried"] == 3


def test_500_then_recovery_uses_exponential_backoff() -> None:
    client, transport, sleeper, clock, telemetry = _client(
        script=[
            http_response(500),
            http_response(503),
            http_response(200, {"ok": True}),
        ]
    )
    outcome = client.execute(_spec())
    assert outcome.status_code == 200
    # backoff: 1*2^0 + jitter(<=1), 1*2^1 + jitter(<=1)
    assert len(sleeper.sleeps) == 2
    assert 1.0 <= sleeper.sleeps[0] <= 2.0
    assert 2.0 <= sleeper.sleeps[1] <= 3.0


def test_non_429_retry_after_uses_normal_backoff() -> None:
    """Only 429 is a provider-wide gate; a 503 header is local backoff."""
    client, _, sleeper, _, _ = _client(
        script=[
            http_response(503, headers={"Retry-After": "30"}),
            http_response(200, {"ok": True}),
        ],
    )
    assert client.execute(_spec()).status_code == 200
    assert len(sleeper.sleeps) == 1
    assert 1.0 <= sleeper.sleeps[0] <= 2.0


def test_shared_gate_deadline_stops_before_http_attempt() -> None:
    """Deadline expiration at Retry-After gate is not permission to send."""
    from src.utils.rate_limit import default_config

    cfg = default_config()
    cfg["global"]["paper_interval_seconds"] = 0.0
    cfg["global"]["jitter_seconds"] = 0.0
    for provider in cfg["providers"].values():
        provider["min_interval_seconds"] = 0.0
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    transport = FakeTransport([http_response(200, {"ok": True})])
    runtime = ProviderRuntime(config=cfg, transport=transport, sleeper=sleeper, clock=clock)
    telemetry = ProviderTelemetry()
    budget = ProviderRequestBudget(limit=1)
    runtime.observe_cooldown("openalex", 10.0)

    with pytest.raises(ProviderTimeoutError):
        runtime.create_client(
            "openalex", telemetry=telemetry, request_budget=budget,
        ).execute(RequestSpec(
            provider="openalex",
            purpose="discovery_page",
            url="https://api.openalex.org/works",
            deadline_monotonic=clock.monotonic() + 5.0,
        ))

    assert transport.request_count == 0
    assert budget.attempted == 0
    assert sleeper.sleeps == [5.0]


def test_shared_gate_cancellation_stops_before_http_attempt() -> None:
    """A cancelled gate wait does not consume budget or bypass cooldown."""
    from src.utils.rate_limit import default_config

    cfg = default_config()
    cfg["global"]["paper_interval_seconds"] = 0.0
    cfg["global"]["jitter_seconds"] = 0.0
    for provider in cfg["providers"].values():
        provider["min_interval_seconds"] = 0.0
    clock = FakeClock()
    transport = FakeTransport([http_response(200, {"ok": True})])
    runtime = ProviderRuntime(
        config=cfg, transport=transport, sleeper=FakeSleeper(clock), clock=clock,
    )
    cancellation = threading.Event()
    cancellation.set()
    budget = ProviderRequestBudget(limit=1)
    runtime.observe_cooldown("openalex", 10.0)

    with pytest.raises(ProviderCancelledError):
        runtime.create_client(
            "openalex", telemetry=ProviderTelemetry(), request_budget=budget,
        ).execute(RequestSpec(
            provider="openalex",
            purpose="discovery_page",
            url="https://api.openalex.org/works",
            cancellation_token=cancellation,
        ))

    assert transport.request_count == 0
    assert budget.attempted == 0


def test_timeout_is_transient_and_retried() -> None:
    client, transport, *_ = _client(
        script=[Fault(Exception("ReadTimeout")), http_response(200, {"ok": True})]
    )
    # Rename the exception class to look like a timeout.
    outcome = client.execute(_spec())
    assert outcome.status_code == 200


def test_named_timeout_classified() -> None:
    class ReadTimeoutError(Exception):
        pass

    client, *_ = _client(script=[Fault(ReadTimeoutError("boom"))], max_retries=0)
    with pytest.raises(ProviderTimeoutError):
        client.execute(_spec())


def test_ssl_error_classified_as_connection() -> None:
    class SSLError(Exception):
        pass

    client, *_ = _client(script=[Fault(SSLError("reset"))], max_retries=0)
    with pytest.raises(ProviderConnectionError):
        client.execute(_spec())


def test_400_permanent_not_retried() -> None:
    client, transport, *_ = _client(script=[http_response(400)], max_retries=3)
    with pytest.raises(ProviderPermanentError):
        client.execute(_spec())
    assert transport.request_count == 1  # no retries on permanent errors


def test_401_auth_not_retried() -> None:
    client, transport, *_ = _client(script=[http_response(401)], max_retries=3)
    with pytest.raises(ProviderAuthError):
        client.execute(_spec())
    assert transport.request_count == 1


def test_malformed_json_is_protocol_error() -> None:
    from src.discovery.providers.provider_client import RawResponse

    client, *_ = _client(script=[RawResponse(200, {}, b"not-json{")])
    outcome = client.execute(_spec())
    with pytest.raises(ProviderProtocolError):
        outcome.json()


def test_request_budget_counts_failures_and_blocks() -> None:
    budget = ProviderRequestBudget(limit=2)
    client, transport, *_ = _client(
        script=[http_response(500), http_response(500), http_response(200)],
        budget=budget,
        max_retries=5,
    )
    with pytest.raises(ProviderRequestBudgetExhausted):
        client.execute(_spec())
    assert budget.attempted == 2  # failed attempts consumed the budget
    assert transport.request_count == 2


def test_circuit_breaker_opens_after_threshold() -> None:
    breaker = CircuitBreaker(failure_threshold=2, recovery_seconds=30.0)
    clock = FakeClock()
    client, *_ = _client(
        script=[http_response(500)] * 10,
        breaker=breaker,
        clock=clock,
        max_retries=0,
    )
    with pytest.raises(ProviderTransientError):
        client.execute(_spec())
    assert breaker.state == "closed"
    with pytest.raises(ProviderTransientError):
        client.execute(_spec())
    assert breaker.state == "open"
    # While open: short-circuit, no transport call.
    with pytest.raises(CircuitOpenError):
        client.execute(_spec())


def test_circuit_breaker_half_open_recovers() -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=30.0)
    clock = FakeClock()
    transport = FakeTransport([http_response(500), http_response(200, {"ok": True})])
    from src.utils.rate_limit import ProviderRateLimiter, default_config

    cfg = default_config()
    cfg["global"]["paper_interval_seconds"] = 0.0
    cfg["global"]["jitter_seconds"] = 0.0
    cfg["providers"]["openalex"]["min_interval_seconds"] = 0.0
    client = ProviderClient(
        "openalex",
        limiter=ProviderRateLimiter(cfg),
        limiter_lock=threading.Lock(),
        breaker=breaker,
        request_budget=None,
        sleeper=FakeSleeper(clock),
        clock=clock,
        transport=transport,
        telemetry=ProviderTelemetry(),
        max_retries=0,
    )
    with pytest.raises(ProviderTransientError):
        client.execute(_spec())
    assert breaker.state == "open"
    clock.advance(31.0)  # past recovery window
    outcome = client.execute(_spec())
    assert outcome.status_code == 200
    assert breaker.state == "closed"


def test_circuit_breaker_shared_across_clients() -> None:
    """Two clients for the same provider share one breaker (runtime wiring)."""
    runtime = ProviderRuntime(
        transport=FakeTransport([http_response(500)] * 12),
        sleeper=FakeSleeper(),
        clock=FakeClock(),
        max_retries=0,
        breaker_failure_threshold=3,
    )
    c1 = runtime.client("openalex")
    c2 = runtime.client("openalex")
    assert c1._breaker is c2._breaker
    for _ in range(3):
        with pytest.raises(ProviderTransientError):
            c1.execute(_spec())
    assert runtime.breaker("openalex").state == "open"
    with pytest.raises(CircuitOpenError):
        c2.execute(_spec())


def test_runtime_clients_share_limiter_per_provider() -> None:
    runtime = ProviderRuntime(transport=FakeTransport([]), sleeper=FakeSleeper(), clock=FakeClock())
    assert runtime.client("openalex")._limiter is runtime.client("openalex")._limiter
    assert runtime.client("openalex")._limiter is not runtime.client("crossref")._limiter


def test_telemetry_tracks_purposes_separately() -> None:
    telemetry = ProviderTelemetry()
    client, *_ = _client(
        script=[http_response(200, {"a": 1}), http_response(200, {"b": 2})],
        telemetry=telemetry,
    )
    client.execute(_spec("discovery_page"))
    client.execute(_spec("title_resolution"))
    snap = telemetry.snapshot()
    assert snap["openalex.discovery_page.succeeded"] == 1
    assert snap["openalex.title_resolution.succeeded"] == 1
    totals = telemetry.totals()
    assert totals["attempted"] == 2
    assert totals["succeeded"] == 2
