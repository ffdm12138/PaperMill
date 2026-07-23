"""Budget primitives for DOI discovery.

Every budget is thread-safe, monotonically consumed, and observable.  The
key structural rule (frozen in the execution contract):

- **Page budgets count logical page operations** (a loop iteration over one
  provider page, whether it re-used a durable journal or issued HTTP).
- **The provider request budget counts real HTTP attempts** (including
  retries and failures) and is shared batch-wide across every purpose
  (discovery pages, title resolution, metadata resolution).

Refresh and backfill page budgets are *separate objects* so the refresh
lane can never consume the backfill lane's page budget (invariant #3).
Budget exhaustion is a clean stop, never a failure.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class BudgetExhausted(Exception):
    """Raised (optionally) when a budget cannot be acquired."""

    def __init__(self, scope: str) -> None:
        super().__init__(f"budget exhausted: {scope}")
        self.scope = scope


class AcquireResult(str, Enum):
    """Typed result of ``DualScopePageBudget.try_acquire()``.

    Replaces the historical ``bool`` return so callers can distinguish
    per-lane caps from batch-total caps without reading private fields.
    """
    ACQUIRED = "acquired"
    LANE_LIMIT_REACHED = "lane_limit_reached"
    BATCH_LIMIT_REACHED = "batch_limit_reached"


@dataclass(frozen=True)
class PageBudgetSnapshot:
    """Immutable point-in-time view of a ``DualScopePageBudget``.

    The coordinator and ReportBuilder consume this -- never private fields.
    """
    per_lane_limit: int | None
    total_limit: int | None
    total_used: int
    lane_used: Mapping[str, int]
    total_exhausted: bool


@dataclass
class DualScopePageBudget:
    """Backfill page budget with both a per-lane and a batch-total scope.

    ``try_acquire(lane_key)`` succeeds only if *both* scopes have remaining
    capacity.  Returns an ``AcquireResult`` so callers never need to inspect
    private fields.  Per-lane usage is tracked per ``lane_key``.
    """

    per_lane_limit: int | None = None
    total_limit: int | None = None
    _total_used: int = 0
    _lane_used: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _runtime_guard: Any = field(default=None, repr=False)

    def _ensure_open(self) -> None:
        if self._runtime_guard is not None:
            self._runtime_guard.ensure_open()

    def try_acquire(self, lane_key: str) -> AcquireResult:
        self._ensure_open()
        with self._lock:
            lane_used = self._lane_used.get(lane_key, 0)
            if self.per_lane_limit is not None and lane_used >= self.per_lane_limit:
                return AcquireResult.LANE_LIMIT_REACHED
            if self.total_limit is not None and self._total_used >= self.total_limit:
                return AcquireResult.BATCH_LIMIT_REACHED
            self._lane_used[lane_key] = lane_used + 1
            self._total_used += 1
            return AcquireResult.ACQUIRED

    def snapshot(self) -> PageBudgetSnapshot:
        """Return an immutable snapshot of the current budget state."""
        with self._lock:
            return PageBudgetSnapshot(
                per_lane_limit=self.per_lane_limit,
                total_limit=self.total_limit,
                total_used=self._total_used,
                lane_used=dict(self._lane_used),
                total_exhausted=(
                    self.total_limit is not None
                    and self._total_used >= self.total_limit
                ),
            )

    def lane_remaining(self, lane_key: str) -> int | None:
        with self._lock:
            if self.per_lane_limit is None:
                return None
            return max(0, self.per_lane_limit - self._lane_used.get(lane_key, 0))

    @property
    def total_remaining(self) -> int | None:
        with self._lock:
            if self.total_limit is None:
                return None
            return max(0, self.total_limit - self._total_used)

    @property
    def total_used(self) -> int:
        with self._lock:
            return self._total_used

    def lane_used(self, lane_key: str) -> int:
        with self._lock:
            return self._lane_used.get(lane_key, 0)


@dataclass
class ProviderRequestBudget:
    """Batch-wide budget counting **real HTTP attempts**.

    Every actual network attempt — including retries and failed requests —
    must acquire this budget before going on the wire (invariant #5).
    ``limit=None`` means no valve; the CLI contract requires at least one
    valve in ``--until-exhausted`` mode.
    """

    limit: int | None = None
    attempted: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _runtime_guard: Any = field(default=None, repr=False)

    def _ensure_open(self) -> None:
        if self._runtime_guard is not None:
            self._runtime_guard.ensure_open()

    def try_acquire(self) -> bool:
        self._ensure_open()
        with self._lock:
            if self.limit is not None and self.attempted >= self.limit:
                return False
            self.attempted += 1
            return True

    @property
    def remaining(self) -> int | None:
        with self._lock:
            if self.limit is None:
                return None
            return max(0, self.limit - self.attempted)

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return self.limit is not None and self.attempted >= self.limit


@dataclass
class BatchDoiResolutionBudget:
    """Batch-level shared budget for Crossref title→DOI resolution.

    Replaces the historical per-drain-call budget (re-initialized on every
    drain).  One instance lives for the whole batch and is threaded through
    every drain call, so a 429 storm stops dispatch batch-wide instead of
    letting each drain re-spend the allowance.
    """

    limit: int
    attempted: int = 0
    resolved: int = 0
    cache_hits: int = 0
    dedup_hits: int = 0
    stopped_by_rate_limit: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _runtime_guard: Any = field(default=None, repr=False)

    def _ensure_open(self) -> None:
        if self._runtime_guard is not None:
            self._runtime_guard.ensure_open()

    def try_acquire(self) -> bool:
        self._ensure_open()
        with self._lock:
            if self.stopped_by_rate_limit:
                return False
            if self.attempted >= self.limit:
                return False
            self.attempted += 1
            return True

    def note_resolved(self) -> None:
        self._ensure_open()
        with self._lock:
            self.resolved += 1

    def note_cache_hit(self) -> None:
        self._ensure_open()
        with self._lock:
            self.cache_hits += 1

    def note_dedup_hit(self) -> None:
        self._ensure_open()
        with self._lock:
            self.dedup_hits += 1

    def stop_for_rate_limit(self) -> None:
        """Freeze dispatch for the rest of the batch (429 observed)."""
        with self._lock:
            self.stopped_by_rate_limit = True

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.limit - self.attempted)

    def snapshot(self) -> dict[str, int | bool]:
        with self._lock:
            return {
                "limit": self.limit,
                "attempted": self.attempted,
                "resolved": self.resolved,
                "cache_hits": self.cache_hits,
                "dedup_hits": self.dedup_hits,
                "stopped_by_rate_limit": self.stopped_by_rate_limit,
            }


@dataclass
class CandidateDrainBudget:
    """Per-keyword candidate drain budget (formerly ``max_candidates``)."""

    limit: int
    used: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _runtime_guard: Any = field(default=None, repr=False)

    def _ensure_open(self) -> None:
        if self._runtime_guard is not None:
            self._runtime_guard.ensure_open()

    def try_acquire(self, n: int = 1) -> bool:
        self._ensure_open()
        with self._lock:
            if self.used + n > self.limit:
                return False
            self.used += n
            return True

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.limit - self.used)

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return self.used >= self.limit
