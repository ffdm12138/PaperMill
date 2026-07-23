"""Phase 0.1: Freeze write-protection regression tests for v99.

After runtime close (via __exit__ or explicit freeze()), the following
components must reject mutations with a typed RuntimeClosedError:

    ProviderClient.execute
    ProviderTelemetry.record_attempt
    ProviderTelemetry.record_retry
    ProviderTelemetry.record_success
    ProviderTelemetry.record_failure

    ProviderRequestBudget.try_acquire
    DualScopePageBudget.try_acquire
    BatchDoiResolutionBudget.try_acquire
    BatchDoiResolutionBudget.note_resolved
    BatchDoiResolutionBudget.note_cache_hit
    BatchDoiResolutionBudget.note_dedup_hit
    CandidateDrainBudget.try_acquire

    ProviderClient.execute
    DiscoveryBatchRuntime.provider_client
    TitleResolutionService.resolve
    CandidateDrainCoordinator.notify
    CandidateDrainCoordinator.drain

Read-only operations MUST still be allowed:

    telemetry.snapshot
    telemetry.totals
    telemetry.snapshot_lane
    budget.snapshot
    runtime frozen snapshots

These tests currently FAIL on the v99 baseline because none of the
guarded components check RuntimeGuard before allowing mutations.
Phase 1 will wire RuntimeGuard to make them pass.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.discovery.runtime.batch_runtime import (
    DiscoveryBatchRuntime,
    RuntimeClosedError,
)
from src.discovery.runtime.budgets import (
    BatchDoiResolutionBudget,
    CandidateDrainBudget,
    DualScopePageBudget,
    ProviderRequestBudget,
)
from src.discovery.providers.provider_telemetry import ProviderTelemetry, TelemetryScope
from src.discovery.runtime.candidate_drain import CandidateDrainCoordinator

pytestmark = pytest.mark.unit


# ── Helper: build a closed runtime ────────────────────────────────────


def _closed_runtime(tmp_path: Path) -> DiscoveryBatchRuntime:
    """Create and close a runtime via context manager for testing."""
    from src.discovery.page_journal import PageJournalStore
    from src.discovery.runtime.batch_runtime import ActiveRelevanceProfiles

    journal = PageJournalStore(tmp_path / "pages")
    runtime = DiscoveryBatchRuntime.create(
        journal=journal,
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        needs_staging=False,
        active_relevance_profiles=ActiveRelevanceProfiles.build({}),
    )
    runtime.__enter__()
    runtime.__exit__(None, None, None)
    return runtime


class _FakeTransport:
    """Transport that raises on any call — execute should be blocked by guard
    before it ever reaches the transport."""
    def request(self, method, url, **kwargs):
        raise AssertionError("transport should not be called after runtime close")


def _ensure_provider_runtime():
    """Ensure ProviderRuntime singleton is initialized for tests that
    need ProviderClient."""
    from src.discovery.providers.provider_client import ProviderRuntime
    try:
        ProviderRuntime.get()
    except Exception:
        ProviderRuntime.reset_for_tests(ProviderRuntime(transport=_FakeTransport()))


# ── Freeze write-protection tests ─────────────────────────────────────


class TestTelemetryRejectsAfterClose:
    """ProviderTelemetry.record_* must raise RuntimeClosedError after close."""

    def test_record_attempt_rejects(self, tmp_path: Path):
        runtime = _closed_runtime(tmp_path)
        scope = TelemetryScope(batch_id="b", lane_id="l", provider="p", purpose="test")
        with pytest.raises(RuntimeClosedError):
            runtime.telemetry.record_attempt(scope)

    def test_record_retry_rejects(self, tmp_path: Path):
        runtime = _closed_runtime(tmp_path)
        scope = TelemetryScope(batch_id="b", lane_id="l", provider="p", purpose="test")
        with pytest.raises(RuntimeClosedError):
            runtime.telemetry.record_retry(scope)

    def test_record_success_rejects(self, tmp_path: Path):
        runtime = _closed_runtime(tmp_path)
        scope = TelemetryScope(batch_id="b", lane_id="l", provider="p", purpose="test")
        with pytest.raises(RuntimeClosedError):
            runtime.telemetry.record_success(scope)

    def test_record_failure_rejects(self, tmp_path: Path):
        runtime = _closed_runtime(tmp_path)
        scope = TelemetryScope(batch_id="b", lane_id="l", provider="p", purpose="test")
        with pytest.raises(RuntimeClosedError):
            runtime.telemetry.record_failure(scope)


class TestTelemetryReadonlyAfterClose:
    """Read-only telemetry operations must still work after close."""

    def test_snapshot_allowed(self, tmp_path: Path):
        runtime = _closed_runtime(tmp_path)
        snap = runtime.telemetry.snapshot()
        assert isinstance(snap, dict)

    def test_totals_allowed(self, tmp_path: Path):
        runtime = _closed_runtime(tmp_path)
        totals = runtime.telemetry.totals()
        assert isinstance(totals, dict)
        assert "attempted" in totals

    def test_snapshot_lane_allowed(self, tmp_path: Path):
        runtime = _closed_runtime(tmp_path)
        result = runtime.telemetry.snapshot_lane("nonexistent")
        assert isinstance(result, dict)


class TestRequestBudgetRejectsAfterClose:
    """ProviderRequestBudget.try_acquire must raise RuntimeClosedError after close."""

    def test_try_acquire_rejects(self, tmp_path: Path):
        runtime = _closed_runtime(tmp_path)
        budget = runtime.request_budget or ProviderRequestBudget(limit=100)
        # Bind guard if standalone budget was created
        if budget._runtime_guard is None:
            budget._runtime_guard = runtime.guard
        with pytest.raises(RuntimeClosedError):
            budget.try_acquire()


class TestPageBudgetRejectsAfterClose:
    """DualScopePageBudget.try_acquire must raise RuntimeClosedError after close."""

    def test_try_acquire_rejects(self, tmp_path: Path):
        runtime = _closed_runtime(tmp_path)
        with pytest.raises(RuntimeClosedError):
            runtime.page_budget.try_acquire("test_lane")

    def test_snapshot_allowed(self, tmp_path: Path):
        runtime = _closed_runtime(tmp_path)
        snap = runtime.page_budget.snapshot()
        assert snap is not None


class TestDoiBudgetRejectsAfterClose:
    """BatchDoiResolutionBudget mutations must raise RuntimeClosedError after close."""

    def test_try_acquire_rejects(self, tmp_path: Path):
        runtime = _closed_runtime(tmp_path)
        budget = runtime.doi_resolution_budget or BatchDoiResolutionBudget(limit=10)
        if budget._runtime_guard is None:
            budget._runtime_guard = runtime.guard
        with pytest.raises(RuntimeClosedError):
            budget.try_acquire()

    def test_note_resolved_rejects(self, tmp_path: Path):
        runtime = _closed_runtime(tmp_path)
        budget = runtime.doi_resolution_budget or BatchDoiResolutionBudget(limit=10)
        if budget._runtime_guard is None:
            budget._runtime_guard = runtime.guard
        with pytest.raises(RuntimeClosedError):
            budget.note_resolved()

    def test_note_cache_hit_rejects(self, tmp_path: Path):
        runtime = _closed_runtime(tmp_path)
        budget = runtime.doi_resolution_budget or BatchDoiResolutionBudget(limit=10)
        if budget._runtime_guard is None:
            budget._runtime_guard = runtime.guard
        with pytest.raises(RuntimeClosedError):
            budget.note_cache_hit()

    def test_note_dedup_hit_rejects(self, tmp_path: Path):
        runtime = _closed_runtime(tmp_path)
        budget = runtime.doi_resolution_budget or BatchDoiResolutionBudget(limit=10)
        if budget._runtime_guard is None:
            budget._runtime_guard = runtime.guard
        with pytest.raises(RuntimeClosedError):
            budget.note_dedup_hit()

    def test_snapshot_allowed(self, tmp_path: Path):
        runtime = _closed_runtime(tmp_path)
        budget = runtime.doi_resolution_budget or BatchDoiResolutionBudget(limit=10)
        snap = budget.snapshot()
        assert isinstance(snap, dict)


class TestCandidateDrainBudgetRejectsAfterClose:
    """CandidateDrainBudget.try_acquire must raise RuntimeClosedError after close."""

    def test_try_acquire_rejects(self, tmp_path: Path):
        runtime = _closed_runtime(tmp_path)
        budget = CandidateDrainBudget(limit=10)
        budget._runtime_guard = runtime.guard
        with pytest.raises(RuntimeClosedError):
            budget.try_acquire()


class TestCandidateDrainCoordinatorRejectsAfterClose:
    """CandidateDrainCoordinator.notify and drain must raise RuntimeClosedError after close."""

    def test_notify_rejects(self, tmp_path: Path):
        runtime = _closed_runtime(tmp_path)
        drain = CandidateDrainCoordinator(
            runtime=runtime,
            journal=None,  # won't be reached — guard rejects first
            options=None,
            worker_id="test",
            paper_raw_dir=tmp_path,
            papers_dir=tmp_path,
            ledger_path=tmp_path / "ledger.json",
            locks_dir=tmp_path / "locks",
            exports_dir=tmp_path / "exports",
        )
        with pytest.raises(RuntimeClosedError):
            drain.notify("test_kw", 1)

    def test_drain_rejects(self, tmp_path: Path):
        runtime = _closed_runtime(tmp_path)
        drain = CandidateDrainCoordinator(
            runtime=runtime,
            journal=None,
            options=None,
            worker_id="test",
            paper_raw_dir=tmp_path,
            papers_dir=tmp_path,
            ledger_path=tmp_path / "ledger.json",
            locks_dir=tmp_path / "locks",
            exports_dir=tmp_path / "exports",
        )
        with pytest.raises(RuntimeClosedError):
            drain.drain("test_kw", 5, phase="test")


class TestProviderClientRejectsAfterClose:
    """ProviderClient.execute and runtime.provider_client() must raise
    RuntimeClosedError after close."""

    def test_provider_client_factory_rejects(self, tmp_path: Path):
        runtime = _closed_runtime(tmp_path)
        _ensure_provider_runtime()
        with pytest.raises(RuntimeClosedError):
            runtime.provider_client("openalex")

    def test_provider_client_execute_rejects(self, tmp_path: Path):
        from src.discovery.providers.provider_client import (
            ProviderRuntime,
            RequestSpec,
        )
        runtime = _closed_runtime(tmp_path)
        _ensure_provider_runtime()
        # We can't use runtime.provider_client() because that rejects,
        # so construct a client directly bound to the closed runtime's guard.
        pr = ProviderRuntime.get()
        client = pr.create_client(
            "openalex",
            telemetry=runtime.telemetry,
            request_budget=runtime.request_budget,
        )
        spec = RequestSpec(
            provider="openalex",
            url="https://api.openalex.org/works?search=test",
            purpose="discovery_page",
            telemetry_tags={"batch_id": runtime.batch_id, "lane_id": "test_lane"},
        )
        with pytest.raises(RuntimeClosedError):
            client.execute(spec)


class TestFreezeIdempotent:
    """Runtime.freeze() can be called multiple times safely."""

    def test_freeze_is_idempotent(self, tmp_path: Path):
        from src.discovery.page_journal import PageJournalStore
        from src.discovery.runtime.batch_runtime import ActiveRelevanceProfiles

        journal = PageJournalStore(tmp_path / "pages")
        runtime = DiscoveryBatchRuntime.create(
            journal=journal,
            paper_raw_dir=tmp_path / "paper_raw",
            papers_dir=tmp_path / "papers",
            ledger_path=tmp_path / "ledger.json",
            needs_staging=False,
            active_relevance_profiles=ActiveRelevanceProfiles.build({}),
        )
        assert not runtime.frozen
        runtime.freeze()
        assert runtime.frozen
        # Second freeze should be a no-op
        runtime.freeze()
        assert runtime.frozen

    def test_frozen_runtime_has_snapshots(self, tmp_path: Path):
        """After freeze, telemetry and page budget snapshots are captured."""
        runtime = _closed_runtime(tmp_path)
        assert runtime._frozen_telemetry is not None
        assert runtime._frozen_page_budget is not None


class TestNormalCompletionDoesNotCancel:
    """Normal batch completion must not set cancellation_token."""

    def test_normal_close_no_cancellation(self, tmp_path: Path):
        from src.discovery.page_journal import PageJournalStore
        from src.discovery.runtime.batch_runtime import ActiveRelevanceProfiles

        journal = PageJournalStore(tmp_path / "pages")
        runtime = DiscoveryBatchRuntime.create(
            journal=journal,
            paper_raw_dir=tmp_path / "paper_raw",
            papers_dir=tmp_path / "papers",
            ledger_path=tmp_path / "ledger.json",
            needs_staging=False,
            active_relevance_profiles=ActiveRelevanceProfiles.build({}),
        )
        runtime.__enter__()
        runtime.__exit__(None, None, None)
        assert runtime.frozen
        assert not runtime.cancellation_token.is_set(), (
            "Normal completion must not set cancellation_token"
        )
        assert runtime.shutdown_reason is not None
        assert runtime.shutdown_reason.value == "completed"
