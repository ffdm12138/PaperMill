"""Direct unit tests for lane executor functions.

These tests verify that execute_refresh_lane and execute_backfill_lane
handle exceptions correctly without double state transitions.

Phase 1 baseline: these tests should FAIL on the current v94 codebase,
confirming the bugs documented in the v94 audit before we fix them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.discovery.execution.lane_models import (
    DiscoveryLaneKey,
    LaneExecutionSpec,
    LaneCounters,
    LaneOutcome,
    LaneState,
    RequestSignature,
    StopReason,
)
from src.discovery.contracts.lane_history import (
    ExhaustionEvidence,
    ProviderResponseMetadata,
)
from src.discovery.execution.lane_state_machine import (
    LaneEvent,
    LaneMachine,
    IllegalTransitionError,
)


def _make_key(mode: str = "refresh") -> DiscoveryLaneKey:
    signature = RequestSignature.create(
        sort="",
        filters={"provider": "openalex", "mode": mode},
        page_size=25,
    )
    return DiscoveryLaneKey(
        keyword_id="k1", query_id="q1", provider="openalex",
        mode=mode, generation=1, request_signature=signature.hash,
    )


def _make_refresh_spec() -> LaneExecutionSpec:
    key = _make_key("refresh")
    signature = RequestSignature.create(
        sort="",
        filters={"provider": "openalex", "mode": "refresh"},
        page_size=25,
    )
    return LaneExecutionSpec(
        key=key,
        request_signature=signature,
        keyword_zh="测试",
        query="test query",
        query_language="en",
        relevance_profile_hash="aaaa000000000000",
        refresh_run_id="refresh-run",
    )


class _RecordingRefreshState:
    def begin_refresh(self, spec: LaneExecutionSpec) -> None:
        self.began = spec

    def complete_refresh(self, spec: LaneExecutionSpec, **kwargs: Any) -> None:
        self.completed = (spec, kwargs)


class TestDirectExecutorExceptionHandling:
    """Verify double state transition bugs (v94 audit problem #8)."""

    def test_permanent_error_does_not_double_transition(self, tmp_path):
        """ProviderPermanentError should produce PERMANENT_FAILED, not IllegalTransitionError."""
        from src.discovery.providers.provider_errors import ProviderPermanentError

        def failing_fetch(*args: Any, **kwargs: Any) -> None:
            raise ProviderPermanentError("test permanent failure")

        spec = _make_refresh_spec()

        try:
            from src.discovery.execution.lane_executor import execute_refresh_lane
            from src.discovery.runtime.batch_runtime import (
                DiscoveryBatchRuntime,
                ActiveRelevanceProfiles,
            )
            from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore
            from src.discovery.stores.page_journal_store import PageJournalStoreV4 as PageJournalStore
            from src.discovery.runtime.budgets import DualScopePageBudget
            from dataclasses import dataclass

            notebook = KeywordNotebookStore(Path(tmp_path))
            journal = PageJournalStore(Path(tmp_path))
            runtime = DiscoveryBatchRuntime.create(
                journal=journal,
                paper_raw_dir=Path(tmp_path),
                papers_dir=Path(tmp_path),
                ledger_path=Path(tmp_path) / "ledger.json",
                needs_staging=False,
                active_relevance_profiles=ActiveRelevanceProfiles.build({}),
                page_budget=DualScopePageBudget(per_lane_limit=10, total_limit=None),
            )

            @dataclass
            class Opts:
                mode: str = "refresh"
                refresh_pages: int = 2
                backfill_pages: int = 2
                max_pages_total: int | None = None
                page_size: int = 25
                until_exhausted: bool = False
                max_provider_requests_total: int | None = None
                staging_no_progress_timeout_seconds: int = 600

            opts = Opts()

            from src.discovery.providers.provider_page_fetcher import CallbackProviderPageFetcher

            outcome = execute_refresh_lane(
                spec,
                runtime=runtime,
                notebook=notebook,
                journal=journal,
                options=opts,
                page_fetcher=CallbackProviderPageFetcher(
                    lambda _spec, _cursor, _client: failing_fetch(),
                ),
                refresh_state=_RecordingRefreshState(),
            )

            # Must NOT be IllegalTransitionError raised
            assert outcome.state == LaneState.PERMANENT_FAILED, (
                f"Expected PERMANENT_FAILED, got {outcome.state}"
            )
        except IllegalTransitionError as e:
            pytest.fail(
                f"IllegalTransitionError raised (v94 bug #8 double transition): {e}"
            )

    def test_timeout_error_does_not_double_transition(self, tmp_path):
        """ProviderError (timeout) should produce RETRYABLE_FAILED, not IllegalTransitionError."""
        from src.discovery.providers.provider_errors import ProviderError

        def failing_fetch(*args: Any, **kwargs: Any) -> None:
            raise ProviderError("test timeout failure")

        spec = _make_refresh_spec()

        try:
            from src.discovery.execution.lane_executor import execute_refresh_lane
            from src.discovery.runtime.batch_runtime import (
                DiscoveryBatchRuntime,
                ActiveRelevanceProfiles,
            )
            from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore
            from src.discovery.stores.page_journal_store import PageJournalStoreV4 as PageJournalStore
            from src.discovery.runtime.budgets import DualScopePageBudget
            from dataclasses import dataclass

            notebook = KeywordNotebookStore(Path(tmp_path))
            journal = PageJournalStore(Path(tmp_path))
            runtime = DiscoveryBatchRuntime.create(
                journal=journal,
                paper_raw_dir=Path(tmp_path),
                papers_dir=Path(tmp_path),
                ledger_path=Path(tmp_path) / "ledger.json",
                needs_staging=False,
                active_relevance_profiles=ActiveRelevanceProfiles.build({}),
                page_budget=DualScopePageBudget(per_lane_limit=10, total_limit=None),
            )

            @dataclass
            class Opts:
                mode: str = "refresh"
                refresh_pages: int = 2
                backfill_pages: int = 2
                max_pages_total: int | None = None
                page_size: int = 25
                until_exhausted: bool = False
                max_provider_requests_total: int | None = None
                staging_no_progress_timeout_seconds: int = 600

            opts = Opts()

            from src.discovery.providers.provider_page_fetcher import CallbackProviderPageFetcher

            outcome = execute_refresh_lane(
                spec,
                runtime=runtime,
                notebook=notebook,
                journal=journal,
                options=opts,
                page_fetcher=CallbackProviderPageFetcher(
                    lambda _spec, _cursor, _client: failing_fetch(),
                ),
                refresh_state=_RecordingRefreshState(),
            )

            assert outcome.state == LaneState.RETRYABLE_FAILED, (
                f"Expected RETRYABLE_FAILED, got {outcome.state}"
            )
        except IllegalTransitionError as e:
            pytest.fail(
                f"IllegalTransitionError raised (v94 bug #8 double transition): {e}"
            )


class TestExhaustionEvidenceInvariant:
    """Verify LaneOutcome exhaustion evidence invariant (v94 audit problem #11)."""

    def test_exhausted_without_evidence_raises(self):
        """LaneOutcome with EXHAUSTED state and None evidence must raise ValueError."""
        with pytest.raises(ValueError, match="exhaustion_evidence"):
            LaneOutcome(
                key=_make_key("backfill"),
                state=LaneState.EXHAUSTED,
                stop_reason=StopReason.PROVIDER_EXHAUSTED,
                counters=LaneCounters(),
                exhaustion_evidence=None,
                errors=(),
            )

    def test_non_exhausted_with_evidence_raises(self):
        """LaneOutcome with non-EXHAUSTED state must not carry evidence."""
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

        with pytest.raises(ValueError, match="exhaustion_evidence"):
            LaneOutcome(
                key=_make_key("backfill"),
                state=LaneState.COMPLETED,
                stop_reason=StopReason.REFRESH_WINDOW_COMPLETE,
                counters=LaneCounters(),
                exhaustion_evidence=evidence,
                errors=(),
            )

    def test_exhausted_with_evidence_constructs(self):
        """Valid EXHAUSTED lane outcome should construct successfully."""
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
            key=_make_key("backfill"),
            state=LaneState.EXHAUSTED,
            stop_reason=StopReason.PROVIDER_EXHAUSTED,
            counters=LaneCounters(pages_durable=1),
            exhaustion_evidence=evidence,
            errors=(),
        )
        assert outcome.state == LaneState.EXHAUSTED
        assert outcome.exhaustion_evidence is not None
        assert outcome.durable_progress is True


class TestTransitionTable:
    """Verify lane state machine transition table invariants."""

    def test_terminal_to_exhausted_is_illegal(self):
        """Once in a non-RUNNING terminal state, PROVIDER_EXHAUSTED transition is illegal."""
        machine = LaneMachine(lane_key=_make_key("backfill"))
        machine.transition(LaneEvent.START)
        # Go to BUDGET_STOPPED first
        machine.transition(LaneEvent.BATCH_PAGE_BUDGET_REACHED)
        assert machine.terminal is True
        # Now try to go to exhausted — illegal
        with pytest.raises(IllegalTransitionError):
            machine.transition(LaneEvent.PROVIDER_EXHAUSTED)

    def test_exhausted_has_correct_stop_reason(self):
        """PROVIDER_EXHAUSTED event must map to PROVIDER_EXHAUSTED stop reason."""
        machine = LaneMachine(lane_key=_make_key("backfill"))
        machine.transition(LaneEvent.START)
        machine.transition(LaneEvent.PROVIDER_EXHAUSTED)
        assert machine.stop_reason == StopReason.PROVIDER_EXHAUSTED
        assert machine.exhausted is True
