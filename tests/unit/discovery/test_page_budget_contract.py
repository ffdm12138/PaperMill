"""Page budget contract tests for v94 discovery.

Phase 1 baseline: these tests verify that the global page budget correctly
limits total pages across all lanes, that already-exhausted lanes don't
pollute durable counters, and that request budget safety valves work.

These tests should FAIL on the current v94 codebase before fixes are applied.
"""

from __future__ import annotations

import pytest


class TestExhaustionEvidenceHardcoding:
    """A successful page must carry real response metadata."""

    def test_missing_response_metadata_is_repair_required(self):
        """There is no status fallback and no fabricated exhaustion evidence."""
        from src.discovery.execution.lane_executor import _response_metadata_from_page
        from src.discovery.page_journal import JournalCorruptError
        from src.discovery.providers.provider_models import DiscoveryPage

        # Create a page with http_status=None (like a recovered page)
        page = DiscoveryPage(
            provider="openalex",
            keyword_zh="test",
            query="test query",
            lane="backfill",
            candidates=[],
            next_cursor=None,
            exhausted=True,
            returned_count=0,
            error_type=None,
            safe_error=None,
            failure_class=None,
        )

        with pytest.raises(JournalCorruptError, match="response_metadata"):
            _response_metadata_from_page(page)


class TestAlreadyExhaustedCount:
    """Verify already-exhausted backfill lanes don't produce fake durable progress (v94 audit problem #12)."""

    def test_already_exhausted_does_not_increment_durable(self):
        """Notebook already has exhausted=True with pages_persisted=0 — pages_durable must be 0."""
        from src.discovery.execution.lane_models import (
            LaneOutcome, LaneState, StopReason, LaneCounters, DiscoveryLaneKey,
            ExhaustionEvidence, ProviderResponseMetadata,
        )

        # EXHAUSTED state requires non-None exhaustion_evidence (invariant enforced in Phase 5)
        evidence = ExhaustionEvidence(
            provider="openalex", query_id="q1",
            request_signature="deadbeef00000000", generation=1,
            cursor_before="*",
            response_metadata=ProviderResponseMetadata(
                http_status=200, provider_request_id=None,
                retry_after_observed=None, total_results=0,
                next_cursor_present=False, response_fingerprint="aaaa000000000000",
                observed_at="2024-01-01T00:00:00+00:00",
            ),
            observed_at="2024-01-01T00:00:00+00:00",
        )

        outcome = LaneOutcome(
            key=DiscoveryLaneKey(
                keyword_id="k1", query_id="q1", provider="openalex",
                mode="backfill", generation=1, request_signature="deadbeef00000000",
            ),
            state=LaneState.EXHAUSTED,
            stop_reason=StopReason.PROVIDER_EXHAUSTED,
            counters=LaneCounters(
                pages_durable=0,
                pages_cursor_committed=0,
            ),
            exhaustion_evidence=evidence,
            errors=(),
        )

        # When nothing new was done (already exhausted from prior run):
        assert outcome.durable_progress is False, (
            "Already-exhausted lane with 0 new pages must not report durable progress"
        )
        assert outcome.counters.pages_durable == 0
        assert outcome.counters.pages_cursor_committed == 0


class TestProviderRequestBudget:
    """Verify provider request budget safety valve behavior (v94 audit problem #6)."""

    def test_request_budget_try_acquire_consumes(self):
        """Each try_acquire must consume budget regardless of outcome."""
        from src.discovery.runtime.budgets import ProviderRequestBudget

        budget = ProviderRequestBudget(limit=3)
        # 3 acquires should succeed
        assert budget.try_acquire() is True
        assert budget.try_acquire() is True
        assert budget.try_acquire() is True
        # 4th should fail
        assert budget.try_acquire() is False
        assert budget.attempted == 3
        assert budget.exhausted is True

    def test_unlimited_request_budget(self):
        """Unlimited budget should never exhaust."""
        from src.discovery.runtime.budgets import ProviderRequestBudget

        budget = ProviderRequestBudget(limit=None)
        for _ in range(1000):
            assert budget.try_acquire() is True
        assert budget.exhausted is False


class TestGlobalPageBudget:
    """Verify global page budget limits all lanes (v94 audit problem #5)."""

    def test_dual_scope_total_limit_shared(self):
        """Total limit should cap all lanes combined, not per-lane."""
        from src.discovery.runtime.budgets import AcquireResult, DualScopePageBudget

        # total_limit=2: only 2 pages across all lanes
        budget = DualScopePageBudget(per_lane_limit=10, total_limit=2)

        # Lane A gets 2 pages
        assert budget.try_acquire("lane-a") == AcquireResult.ACQUIRED
        assert budget.try_acquire("lane-a") == AcquireResult.ACQUIRED
        # Lane A tries 3rd — fails due to total limit
        assert budget.try_acquire("lane-a") == AcquireResult.BATCH_LIMIT_REACHED

        # Lane B gets 0
        assert budget.try_acquire("lane-b") == AcquireResult.BATCH_LIMIT_REACHED
        assert budget.total_used == 2

    def test_per_lane_limit_independent_of_total(self):
        """Per-lane limit should cap individual lanes without affecting others."""
        from src.discovery.runtime.budgets import AcquireResult, DualScopePageBudget

        budget = DualScopePageBudget(per_lane_limit=2, total_limit=10)

        # Lane A fills its per-lane cap
        assert budget.try_acquire("lane-a") == AcquireResult.ACQUIRED
        assert budget.try_acquire("lane-a") == AcquireResult.ACQUIRED
        assert budget.try_acquire("lane-a") == AcquireResult.LANE_LIMIT_REACHED  # per-lane cap

        # Lane B can still acquire (total not yet exhausted)
        assert budget.try_acquire("lane-b") == AcquireResult.ACQUIRED
        assert budget.total_used == 3
