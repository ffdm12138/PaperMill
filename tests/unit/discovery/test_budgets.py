"""Unit tests for the discovery budget primitives."""
from __future__ import annotations

import threading

from src.discovery.runtime.budgets import (
    AcquireResult,
    BatchDoiResolutionBudget,
    CandidateDrainBudget,
    DualScopePageBudget,
    ProviderRequestBudget,
)


def test_dual_scope_budget_counts_and_exhausts() -> None:
    b = DualScopePageBudget(per_lane_limit=2)
    assert b.try_acquire("lane-a") == AcquireResult.ACQUIRED
    assert b.try_acquire("lane-a") == AcquireResult.ACQUIRED
    assert b.try_acquire("lane-a") == AcquireResult.LANE_LIMIT_REACHED
    assert b.total_used == 2


def test_dual_scope_budget_total_unlimited() -> None:
    b = DualScopePageBudget(total_limit=None)
    for _ in range(100):
        assert b.try_acquire("lane-a") == AcquireResult.ACQUIRED
    assert b.total_used == 100


def test_dual_scope_budget_isolates_lanes() -> None:
    b = DualScopePageBudget(per_lane_limit=2, total_limit=3)
    assert b.try_acquire("lane-a") == AcquireResult.ACQUIRED
    assert b.try_acquire("lane-a") == AcquireResult.ACQUIRED
    # Per-lane cap reached for lane-a even though total remains.
    assert b.try_acquire("lane-a") == AcquireResult.LANE_LIMIT_REACHED
    assert b.try_acquire("lane-b") == AcquireResult.ACQUIRED
    # Total cap (3) now reached.
    assert b.try_acquire("lane-c") == AcquireResult.BATCH_LIMIT_REACHED
    assert b.lane_used("lane-a") == 2
    assert b.total_used == 3
    assert b.lane_remaining("lane-a") == 0
    assert b.total_remaining == 0


def test_dual_scope_budget_snapshot() -> None:
    """PageBudgetSnapshot must reflect the correct budget state."""
    b = DualScopePageBudget(per_lane_limit=5, total_limit=10)
    snap0 = b.snapshot()
    assert snap0.total_used == 0
    assert snap0.total_limit == 10
    assert snap0.per_lane_limit == 5
    assert snap0.total_exhausted is False
    assert snap0.lane_used == {}

    b.try_acquire("lane-a")
    b.try_acquire("lane-a")
    b.try_acquire("lane-b")
    snap1 = b.snapshot()
    assert snap1.total_used == 3
    assert snap1.lane_used == {"lane-a": 2, "lane-b": 1}
    assert snap1.total_exhausted is False

    # Exhaust the total limit.
    b2 = DualScopePageBudget(total_limit=0)
    assert b2.snapshot().total_exhausted is True


def test_provider_request_budget_counts_failures_too() -> None:
    """Every HTTP attempt — success or failure — consumes the budget."""
    b = ProviderRequestBudget(limit=3)
    for _ in range(3):
        assert b.try_acquire()
    assert not b.try_acquire()
    assert b.attempted == 3
    assert b.exhausted


def test_doi_resolution_budget_is_batch_level_and_stops_on_429() -> None:
    b = BatchDoiResolutionBudget(limit=2)
    assert b.try_acquire()
    b.note_resolved()
    b.note_cache_hit()
    b.note_dedup_hit()
    assert b.try_acquire()
    assert not b.try_acquire()  # limit reached
    snap = b.snapshot()
    assert snap == {
        "limit": 2,
        "attempted": 2,
        "resolved": 1,
        "cache_hits": 1,
        "dedup_hits": 1,
        "stopped_by_rate_limit": False,
    }
    b.stop_for_rate_limit()
    # After a 429, dispatch stays frozen even though limit not re-checked.
    b2 = BatchDoiResolutionBudget(limit=10)
    assert b2.try_acquire()
    b2.stop_for_rate_limit()
    assert not b2.try_acquire()
    assert b2.snapshot()["stopped_by_rate_limit"] is True


def test_candidate_drain_budget() -> None:
    b = CandidateDrainBudget(limit=5)
    assert b.try_acquire(3)
    assert b.remaining == 2
    assert not b.try_acquire(3)
    assert b.try_acquire(2)
    assert b.exhausted


def test_budgets_are_thread_safe() -> None:
    b = ProviderRequestBudget(limit=100)
    acquired = []

    def worker() -> None:
        local = 0
        while b.try_acquire():
            local += 1
        acquired.append(local)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(acquired) == 100
    assert b.attempted == 100
