"""Shared provider Retry-After gate for DOI discovery.

Every provider worker shares one gate per provider.  When any worker
receives a ``429 Retry-After`` response, all workers wait at the gate
before their next attempt — instead of each worker sleeping independently
(which would let other workers race into the cooldown window).

The gate is separate from the circuit breaker: the breaker reflects the
client's own decision to stop after too many consecutive failures, while
the gate reflects a provider-requested pause.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class GateResult(str, Enum):
    """Result of ``SharedProviderGate.wait_until_allowed()``."""
    ALLOWED = "allowed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class Clock(Protocol):
    """Injective monotonic clock for deterministic testing."""
    def monotonic(self) -> float: ...


class _TimeClock:
    """Production clock — wraps ``time.monotonic``."""
    def monotonic(self) -> float:
        import time
        return time.monotonic()


class Waiter(Protocol):
    """Injective condition-waiter for deterministic testing.

    A real waiter calls ``condition.wait(timeout)``; a test waiter
    advances the fake clock then returns.
    """
    def wait(self, condition: threading.Condition, timeout: float) -> bool: ...


class _ConditionWaiter:
    """Production waiter — delegates to ``threading.Condition.wait``."""
    def wait(self, condition: threading.Condition, timeout: float) -> bool:
        return condition.wait(timeout=timeout)


@dataclass(frozen=True)
class SleeperWaiter:
    """Clock-advancing waiter for deterministic tests.

    The production gate uses a condition waiter.  Tests with a fake monotonic
    clock inject this waiter so a Retry-After window makes forward progress
    without wall-clock sleeping.
    """

    sleeper: object

    def wait(self, condition: threading.Condition, timeout: float) -> bool:
        # ``Condition.wait`` with zero releases the lock while retaining the
        # condition protocol.  The injected sleeper then advances fake time.
        condition.wait(timeout=0)
        getattr(self.sleeper, "sleep")(timeout)
        return True


@dataclass
class SharedProviderGate:
    """Per-provider Retry-After cooldown gate, shared by all workers.

    Workers call ``wait_until_allowed(provider)`` before every HTTP attempt.
    If another worker observed a ``Retry-After``, the caller waits until
    the cooldown expires.

    Uses a ``max(current, new)`` rule for cooldown deadlines so a shorter
    cooldown never overwrites a longer one.
    """

    clock: Clock = field(default_factory=_TimeClock)
    waiter: Waiter = field(default_factory=_ConditionWaiter)

    _cooldowns: dict[str, float] = field(default_factory=dict, repr=False)
    _condition: threading.Condition = field(default_factory=lambda: threading.Condition(), repr=False)

    def wait_until_allowed(
        self,
        provider: str,
        *,
        deadline: float | None = None,
        cancellation_token: threading.Event | None = None,
    ) -> GateResult:
        """Block until *provider* cooldown expires or the caller is cancelled.

        Returns:
            ``ALLOWED`` when cooldown expires (or no cooldown active).
            ``CANCELLED`` when the cancellation token is set.
            ``TIMED_OUT`` when the caller's deadline expires before the gate.
        """
        with self._condition:
            while True:
                # Check cancellation first.
                if cancellation_token is not None and cancellation_token.is_set():
                    return GateResult.CANCELLED

                now = self.clock.monotonic()
                deadline_at = self._cooldowns.get(provider)

                # No cooldown, or it has expired.
                if deadline_at is None:
                    return GateResult.ALLOWED
                remaining = deadline_at - now
                if remaining <= 0:
                    self._cooldowns.pop(provider, None)
                    return GateResult.ALLOWED

                # Bound wait to batch deadline if provided.
                wait = remaining
                if deadline is not None:
                    wait = min(wait, max(0.0, deadline - now))
                    if wait <= 0:
                        return GateResult.TIMED_OUT

                # ``threading.Event`` cannot wake a Condition by itself.
                # Bound a cancellable wait so a cancellation made by another
                # worker is observed promptly, while ordinary cooldown waits
                # remain one condition wait with no polling overhead.
                if cancellation_token is not None:
                    wait = min(wait, 0.1)

                self.waiter.wait(self._condition, timeout=wait)

    def observe_cooldown(self, provider: str, retry_after_seconds: float) -> None:
        """Record a shared cooldown deadline for *provider*.

        Uses ``max(current, new)`` so a shorter cooldown never overwrites
        a longer one.  Notifies all waiting workers so they can re-check
        the (possibly extended) deadline.
        """
        if retry_after_seconds <= 0:
            return
        with self._condition:
            current = self._cooldowns.get(provider, 0.0)
            new_deadline = self.clock.monotonic() + retry_after_seconds
            if new_deadline > current:
                self._cooldowns[provider] = new_deadline
            self._condition.notify_all()
