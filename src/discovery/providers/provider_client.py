"""Unified provider request runtime for DOI discovery.

This module is the **only** place where OpenAlex/Crossref HTTP requests are
issued.  Business modules must construct a :class:`RequestSpec` and call
:meth:`ProviderClient.execute`; they must never import ``requests`` or open
sockets themselves (enforced by a hygiene contract test).

The runtime provides, for every request regardless of purpose:

- provider-scoped rate limiting (shared :class:`ProviderRateLimiter`);
- a batch-wide :class:`ProviderRequestBudget` counting **real HTTP
  attempts** (including retries and failures);
- retry with exponential backoff + jitter, honoring ``Retry-After``;
- a process-wide circuit breaker per provider (shared across workers);
- typed :mod:`src.discovery.provider_errors` failures — never strings;
- telemetry (attempted/retried/succeeded/failed per provider+purpose).

Testability: ``Transport``, ``Sleeper`` and ``Clock`` are injectable, so
fault-injection and property tests run with zero real network and zero
real sleeping.
"""
from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Protocol

from src.discovery.runtime.budgets import ProviderRequestBudget
from src.discovery.providers.provider_errors import (
    CircuitOpenError,
    ProviderAuthError,
    ProviderCancelledError,
    ProviderConnectionError,
    ProviderError,
    ProviderPermanentError,
    ProviderProtocolError,
    ProviderRateLimited,
    ProviderRequestBudgetExhausted,
    ProviderTimeoutError,
    ProviderTransientError,
    classify_response_failure,
    classify_transport_error,
)
from src.discovery.providers.provider_gate import GateResult, SharedProviderGate, SleeperWaiter, Waiter
from src.discovery.providers.provider_telemetry import (
    ProviderTelemetry,
    TelemetryScope,
)
from src.services.rate_limit import ProviderRateLimiter, default_config, load_config

RequestPurpose = Literal["discovery_page", "title_resolution", "metadata_resolution"]

#: Providers known to the discovery runtime.
KNOWN_PROVIDERS = ("openalex", "crossref")

#: Retry policy defaults (safe for long-unattended runs).
DEFAULT_MAX_RETRIES = 5
DEFAULT_BACKOFF_INITIAL_SECONDS = 1.0
DEFAULT_BACKOFF_MULTIPLIER = 2.0
DEFAULT_BACKOFF_MAX_SECONDS = 300.0


# ── Abstractions ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class RawResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


class Transport(Protocol):
    """Synchronous HTTP transport.  The only place raw HTTP may live."""

    def send(self, spec: "RequestSpec", timeout_seconds: float) -> RawResponse: ...


class Sleeper(Protocol):
    def sleep(self, seconds: float) -> None: ...


class Clock(Protocol):
    def monotonic(self) -> float: ...


class _TimeSleeper:
    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class _TimeClock:
    def monotonic(self) -> float:
        return time.monotonic()


class RequestsTransport:
    """Production transport built on ``requests`` (the only requests user)."""

    def __init__(self, proxies: dict[str, str] | None = None) -> None:
        self._proxies = proxies

    def send(self, spec: "RequestSpec", timeout_seconds: float) -> RawResponse:
        import requests  # local import: the single allowed requests call-site

        response = requests.get(
            spec.url,
            params=spec.params,
            headers=spec.headers,
            timeout=timeout_seconds,
            proxies=self._proxies,
        )
        return RawResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
            body=response.content,
        )


# ── Request / outcome models ──────────────────────────────────────────


@dataclass(frozen=True)
class RequestSpec:
    provider: str
    purpose: RequestPurpose
    url: str
    params: Mapping[str, Any] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    method: str = "GET"
    timeout_seconds: float = 30.0
    telemetry_tags: Mapping[str, str] = field(default_factory=dict)
    # A gate deadline/cancellation must prevent a new HTTP attempt; it is not
    # permission to bypass Retry-After.
    deadline_monotonic: float | None = None
    cancellation_token: threading.Event | None = None


@dataclass(frozen=True)
class RequestOutcome:
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    attempts: int
    retries: int
    retry_after_observed: float | None
    elapsed_seconds: float

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderProtocolError(
                f"malformed JSON response: {exc}"
            ) from exc


# ── Circuit breaker ───────────────────────────────────────────────────


@dataclass
class CircuitBreaker:
    """Per-provider circuit breaker, shared across all workers.

    Opens after ``failure_threshold`` consecutive transient failures; while
    open it short-circuits requests until ``recovery_seconds`` have passed,
    then allows a single half-open probe.
    """

    failure_threshold: int = 5
    recovery_seconds: float = 60.0
    half_open_max_probes: int = 1
    _state: str = "closed"  # closed | open | half_open
    _consecutive_failures: int = 0
    _opened_at: float = 0.0
    _half_open_probes: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def before_request(self, now: float) -> None:
        with self._lock:
            if self._state == "open":
                if now - self._opened_at >= self.recovery_seconds:
                    self._state = "half_open"
                    self._half_open_probes = 0
                else:
                    raise CircuitOpenError("provider circuit breaker is open")
            if self._state == "half_open":
                if self._half_open_probes >= self.half_open_max_probes:
                    raise CircuitOpenError("provider circuit half-open probe in flight")
                self._half_open_probes += 1

    def record_success(self) -> None:
        with self._lock:
            self._state = "closed"
            self._consecutive_failures = 0
            self._half_open_probes = 0

    def record_failure(self, now: float) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._state == "half_open":
                # Failed probe: re-open.
                self._state = "open"
                self._opened_at = now
                self._half_open_probes = 0
                return
            if self._consecutive_failures >= self.failure_threshold:
                self._state = "open"
                self._opened_at = now


# ── ProviderClient ────────────────────────────────────────────────────


def _parse_retry_after(headers: Mapping[str, str]) -> float | None:
    for key, value in (headers or {}).items():
        if str(key).lower() == "retry-after":
            try:
                return float(str(value).strip())
            except (TypeError, ValueError):
                return None
    return None


class ProviderClient:
    """Unified, purpose-tagged provider request client."""

    def __init__(
        self,
        provider: str,
        *,
        limiter: ProviderRateLimiter,
        limiter_lock: threading.Lock,
        breaker: CircuitBreaker,
        request_budget: ProviderRequestBudget | None,
        sleeper: Sleeper,
        clock: Clock,
        transport: Transport,
        telemetry: ProviderTelemetry,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_initial_seconds: float = DEFAULT_BACKOFF_INITIAL_SECONDS,
        backoff_multiplier: float = DEFAULT_BACKOFF_MULTIPLIER,
        backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
        rng: random.Random | None = None,
        # Phase 6: shared Retry-After cooldown callbacks (from ProviderRuntime).
        cooldown_check: Callable[..., GateResult | None] | None = None,
        cooldown_observe: Callable[[str, float], None] | None = None,
        # v99: batch-scoped runtime guard for freeze write-protection.
        runtime_guard: Any | None = None,
    ) -> None:
        self.provider = provider
        self._runtime_guard = runtime_guard
        self._limiter = limiter
        self._limiter_lock = limiter_lock
        self._breaker = breaker
        self._budget = request_budget
        self._sleeper = sleeper
        self._clock = clock
        self._transport = transport
        self._telemetry = telemetry
        self._max_retries = max(0, int(max_retries))
        self._backoff_initial = float(backoff_initial_seconds)
        self._backoff_multiplier = float(backoff_multiplier)
        self._backoff_max = float(backoff_max_seconds)
        self._rng = rng or random.Random()
        self._cooldown_check = cooldown_check or (lambda _p, **_kw: None)
        self._cooldown_observe = cooldown_observe or (lambda _p, _r: None)
        self._shared_cooldown = cooldown_check is not None and cooldown_observe is not None

    # ── public API ────────────────────────────────────────────────────

    def execute(self, spec: RequestSpec) -> RequestOutcome:
        """Execute one logical request with retry/backoff/classification."""
        if self._runtime_guard is not None:
            self._runtime_guard.ensure_open()
        started = self._clock.monotonic()
        attempts = 0
        retries = 0
        retry_after_observed: float | None = None
        last_error: ProviderError | None = None

        # Build lane-aware telemetry scope from spec tags
        tags = dict(spec.telemetry_tags or {})
        batch_id = tags.get("batch_id")
        lane_id = tags.get("lane_id") or None
        operation_id = tags.get("operation_id") or None
        if batch_id is None:
            # Standalone (non-batch) operation — auto-assign a unique id.
            import uuid as _uuid
            batch_id = f"standalone:{_uuid.uuid4().hex[:12]}"
        if lane_id is None and operation_id is None:
            import uuid as _uuid
            operation_id = f"standalone:{_uuid.uuid4().hex[:12]}"
        if lane_id is not None and operation_id is not None:
            raise ValueError(
                "ProviderClient.execute(): exactly one of lane_id / operation_id "
                "must be set"
            )
        telemetry_scope = TelemetryScope(
            batch_id=batch_id,
            provider=spec.provider,
            purpose=spec.purpose,
            lane_id=lane_id,
            operation_id=operation_id,
        )

        for attempt_index in range(self._max_retries + 1):
            # Every attempt is gated before the request budget is consumed.
            # Deadline/cancellation may stop waiting, but can never authorize
            # an attempt inside a provider-declared cooldown window.
            gate_result = self._cooldown_check(
                self.provider,
                deadline=spec.deadline_monotonic,
                cancellation_token=spec.cancellation_token,
            )
            if gate_result == GateResult.CANCELLED:
                raise ProviderCancelledError(
                    "provider request cancelled while waiting for Retry-After gate",
                    provider=self.provider,
                    purpose=spec.purpose,
                )
            if gate_result == GateResult.TIMED_OUT:
                raise ProviderTimeoutError(
                    "provider Retry-After gate exceeded request deadline",
                    provider=self.provider,
                    purpose=spec.purpose,
                )
            # Circuit breaker gate (shared across workers).
            self._breaker.before_request(self._clock.monotonic())
            # Budget gate: every real HTTP attempt must be accounted.
            if self._budget is not None and not self._budget.try_acquire():
                raise ProviderRequestBudgetExhausted(
                    f"provider request budget exhausted for {self.provider}"
                )
            attempts += 1
            self._telemetry.record_attempt(telemetry_scope)
            if attempt_index > 0:
                retries += 1
                self._telemetry.record_retry(telemetry_scope)

            try:
                with self._limiter_lock:
                    self._limiter.wait(self.provider)
                raw = self._transport.send(spec, spec.timeout_seconds)
            except ProviderError as pe:
                self._telemetry.record_failure(telemetry_scope)
                raise
            except Exception as exc:  # transport-level failure
                last_error = classify_transport_error(
                    exc, provider=self.provider, purpose=spec.purpose
                )
                self._breaker.record_failure(self._clock.monotonic())
                if attempt_index >= self._max_retries or not last_error.retryable:
                    self._telemetry.record_failure(telemetry_scope)
                    raise last_error from exc
                self._sleeper.sleep(self._retry_delay(attempt_index, None))
                continue

            # HTTP response received.
            with self._limiter_lock:
                try:
                    self._limiter.record_response(
                        self.provider, dict(raw.headers), raw.status_code
                    )
                except Exception:
                    pass  # adaptive header parsing must never break the lane

            if 200 <= raw.status_code < 300:
                self._breaker.record_success()
                self._telemetry.record_success(telemetry_scope)
                return RequestOutcome(
                    status_code=raw.status_code,
                    headers=raw.headers,
                    body=raw.body,
                    attempts=attempts,
                    retries=retries,
                    retry_after_observed=retry_after_observed,
                    elapsed_seconds=self._clock.monotonic() - started,
                )

            retry_after = _parse_retry_after(raw.headers)
            if retry_after is not None:
                retry_after_observed = retry_after
                # Only a 429 creates the provider-wide gate.  Other HTTP
                # failures may carry Retry-After but follow ordinary local
                # retry/backoff so they cannot freeze unrelated lanes.
                if raw.status_code == 429:
                    self._cooldown_observe(self.provider, retry_after)
            last_error = classify_response_failure(
                raw.status_code,
                retry_after_seconds=retry_after,
                provider=self.provider,
                purpose=spec.purpose,
            )
            if not last_error.retryable:
                # Permanent/auth failures do not trip the transient breaker.
                self._telemetry.record_failure(telemetry_scope)
                raise last_error
            # Only record breaker failure for service/transport failures
            # (5xx, timeout, connection reset).  Rate limits (429) update
            # the shared cooldown gate but must NOT trip the breaker.
            if raw.status_code != 429:
                self._breaker.record_failure(self._clock.monotonic())
            if attempt_index >= self._max_retries:
                self._telemetry.record_failure(telemetry_scope)
                raise last_error
            if (
                raw.status_code == 429
                and retry_after is not None
                and self._shared_cooldown
            ):
                # Do not sleep here.  The next loop iteration waits at the
                # shared gate, so every concurrent worker observes one common
                # cooldown instead of stacking independent delays.
                continue
            # A Retry-After header on a non-429 response is not a shared
            # provider gate.  It follows ordinary exponential backoff rather
            # than freezing unrelated lanes or overriding the local policy.
            # Direct non-runtime test clients retain a local 429 sleep so
            # they remain usable without a shared gate; production batch
            # clients always take the branch above.
            local_retry_after = (
                retry_after
                if raw.status_code == 429 and retry_after is not None
                else None
            )
            self._sleeper.sleep(self._retry_delay(attempt_index, local_retry_after))

        # Unreachable, but keeps type-checkers happy.
        assert last_error is not None
        raise last_error

    # ── internals ─────────────────────────────────────────────────────

    def _retry_delay(self, attempt_index: int, retry_after: float | None) -> float:
        if retry_after is not None and retry_after > 0:
            return float(retry_after)
        base = min(
            self._backoff_initial * (self._backoff_multiplier ** attempt_index),
            self._backoff_max,
        )
        jitter = self._rng.uniform(0.0, self._backoff_initial)
        return base + jitter


# ── Process-wide runtime (singleton) ──────────────────────────────────


class ProviderRuntime:
    """Process-level singleton wiring limiters, breakers and clients.

    All workers in a batch share the same limiter, circuit breaker, and
    Retry-After cooldown per provider, so a 429 storm opens one breaker
    for the whole process, every lane backs off together, and *all* workers
    observe the cooldown window rather than sleeping independently.

    The Retry-After cooldown and circuit breaker are distinct mechanisms:
    cooldown reflects a provider-requested pause (``Retry-After`` header),
    while the breaker reflects the client's own decision to stop after too
    many consecutive transient failures.  Both can be active simultaneously.
    """

    _instance: "ProviderRuntime | None" = None
    _instance_lock = threading.Lock()

    def __init__(
        self,
        *,
        config: dict[str, Any] | None = None,
        transport: Transport | None = None,
        sleeper: Sleeper | None = None,
        clock: Clock | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        breaker_failure_threshold: int = 5,
        breaker_recovery_seconds: float = 60.0,
        gate_waiter: Waiter | None = None,
    ) -> None:
        self._config = config or default_config()
        self._sleeper = sleeper or _TimeSleeper()
        self._clock = clock or _TimeClock()
        self._transport = transport or RequestsTransport()
        self._max_retries = max_retries
        self.telemetry = ProviderTelemetry()
        self._limiters: dict[str, ProviderRateLimiter] = {
            provider: ProviderRateLimiter(self._config) for provider in KNOWN_PROVIDERS
        }
        self._limiter_locks: dict[str, threading.Lock] = {
            provider: threading.Lock() for provider in KNOWN_PROVIDERS
        }
        self._breakers: dict[str, CircuitBreaker] = {
            provider: CircuitBreaker(
                failure_threshold=breaker_failure_threshold,
                recovery_seconds=breaker_recovery_seconds,
            )
            for provider in KNOWN_PROVIDERS
        }
        #: Shared Retry-After gate (per-provider cooldown).
        if gate_waiter is None and not isinstance(self._clock, _TimeClock):
            gate_waiter = SleeperWaiter(self._sleeper)
        self._gate = (
            SharedProviderGate(clock=self._clock)
            if gate_waiter is None
            else SharedProviderGate(clock=self._clock, waiter=gate_waiter)
        )

    # ── singleton management ──────────────────────────────────────────

    @classmethod
    def get(cls) -> "ProviderRuntime":
        """Return the process-wide runtime, creating it on first use.

        The runtime configuration is loaded from
        ``config/metadata_rate_limits.json`` when present (this fixes the
        historical ``load_config`` dead code), falling back to defaults.
        The shared HTTP transport is wired with the project-wide fetch
        proxy (``src.fetch.proxy.get_fetch_proxies``) so every discovery /
        title-resolution / metadata-resolution request respects it -
        replacing the per-module ``proxies=get_fetch_proxies()`` calls that
        the unified client consolidated.
        """
        with cls._instance_lock:
            if cls._instance is None:
                from config.settings import PROJECT_ROOT
                from src.fetch.proxy import get_fetch_proxies

                cls._instance = cls(
                    config=load_config(str(PROJECT_ROOT / "config" / "metadata_rate_limits.json")),
                    transport=RequestsTransport(proxies=get_fetch_proxies()),
                )
            return cls._instance

    @classmethod
    def reset_for_tests(cls, runtime: "ProviderRuntime | None" = None) -> "ProviderRuntime":
        """Replace (or clear) the singleton — test seam."""
        with cls._instance_lock:
            cls._instance = runtime or cls()
            return cls._instance

    # ── client factory ────────────────────────────────────────────────

    def client(self, provider: str) -> ProviderClient:
        """Return a client bound to the process-wide telemetry (no batch budget).

        Only for test/non-batch callers.  Batch callers MUST use
        ``create_client()`` or ``DiscoveryBatchRuntime.provider_client()``.
        """
        if provider not in KNOWN_PROVIDERS:
            raise ValueError(f"unknown provider: {provider!r}")
        return ProviderClient(
            provider,
            limiter=self._limiters[provider],
            limiter_lock=self._limiter_locks[provider],
            breaker=self._breakers[provider],
            request_budget=None,
            sleeper=self._sleeper,
            clock=self._clock,
            transport=self._transport,
            telemetry=self.telemetry,
            max_retries=self._max_retries,
            cooldown_check=self.check_cooldown,
            cooldown_observe=self.observe_cooldown,
        )

    def create_client(
        self, provider: str, *,
        telemetry: ProviderTelemetry,
        request_budget: ProviderRequestBudget | None,
        runtime_guard: Any | None = None,
    ) -> ProviderClient:
        """Create a ``ProviderClient`` bound to batch-scoped telemetry and budget.

        This is the batch-safe factory: all shared infrastructure (transport,
        limiter, breaker, cooldown) comes from the process singleton; the
        caller injects batch-scoped telemetry, budget, and runtime guard.
        """
        if provider not in KNOWN_PROVIDERS:
            raise ValueError(f"unknown provider: {provider!r}")
        return ProviderClient(
            provider,
            limiter=self._limiters[provider],
            limiter_lock=self._limiter_locks[provider],
            breaker=self._breakers[provider],
            request_budget=request_budget,
            sleeper=self._sleeper,
            clock=self._clock,
            transport=self._transport,
            telemetry=telemetry,
            max_retries=self._max_retries,
            cooldown_check=self.check_cooldown,
            cooldown_observe=self.observe_cooldown,
            runtime_guard=runtime_guard,
        )

    def limiter(self, provider: str) -> ProviderRateLimiter:
        return self._limiters[provider]

    def breaker(self, provider: str) -> CircuitBreaker:
        return self._breakers[provider]

    # ── Shared Retry-After cooldown ────────────────────────────────────

    def check_cooldown(
        self,
        provider: str,
        *,
        deadline: float | None = None,
        cancellation_token: threading.Event | None = None,
    ) -> GateResult:
        """Block until *provider* cooldown expires (wait, don't fail).

        Delegates to ``SharedProviderGate.wait_until_allowed()``.
        All workers block together; no worker fails just because another
        observed a Retry-After.
        """
        return self._gate.wait_until_allowed(
            provider,
            deadline=deadline,
            cancellation_token=cancellation_token,
        )

    def observe_cooldown(self, provider: str, retry_after_seconds: float) -> None:
        """Record a shared cooldown deadline for *provider*.

        Delegates to ``SharedProviderGate.observe_cooldown()``.
        Uses ``max(current, new)`` so a shorter cooldown never overwrites
        a longer one.
        """
        self._gate.observe_cooldown(provider, retry_after_seconds)
