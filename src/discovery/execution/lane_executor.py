"""Typed executor for one physical DOI-discovery lane.

The coordinator creates an immutable :class:`LaneExecutionSpec` before work is
submitted.  This module consumes that exact spec; it never rebuilds request
identity, filters, generation, or a request signature from loose arguments.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from src.discovery.backfill_transaction import (
    BackfillTransactionResult,
    StateLockTimeout,
    run_backfill_page_transaction,
)
from src.discovery.runtime.batch_runtime import DiscoveryBatchRuntime
from src.discovery.runtime.budgets import AcquireResult
from src.discovery.constants import INITIAL_CURSOR
from src.discovery.contracts.notebook import CursorConflictError
from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore
from src.discovery.execution.lane_models import (
    DiscoveryLaneKey,
    LaneCounters,
    LaneError,
    LaneExecutionSpec,
    LaneOutcome,
    LaneState,
    StopReason,
)
from src.discovery.contracts.lane_history import (
    ExhaustionEvidence,
    ProviderResponseMetadata,
)
from src.discovery.execution.lane_services import RefreshStateService
from src.discovery.execution.lane_state_machine import LaneEvent, LaneMachine
from src.discovery.contracts.page_journal import JournalCorruptError, refresh_page_id
from src.discovery.stores.page_journal_store import PageJournalStoreV4 as PageJournalStore
from src.discovery.providers.provider_client import ProviderClient
from src.discovery.providers.provider_errors import (
    CircuitOpenError,
    ProviderError,
    ProviderPermanentError,
    ProviderRequestBudgetExhausted,
)
from src.discovery.providers.provider_models import DiscoveryPage
from src.discovery.providers.provider_page_fetcher import ProviderPageFetcher


CandidateBudgetExhausted = Callable[[str], bool]
StagingNotifier = Callable[[str, int], None]
PagePersisted = Callable[[Path, dict[str, Any]], None]
PageFinalizer = Callable[[Path], Any]


def _typed_exception_to_lane_event(exc: Exception) -> tuple[LaneEvent, str]:
    """Map a caught typed failure onto the sole lane state machine."""
    if isinstance(exc, StateLockTimeout):
        return LaneEvent.STATE_LOCK_TIMEOUT, str(exc)
    if isinstance(exc, ProviderPermanentError):
        return LaneEvent.PERMANENT_FAILURE, str(exc)
    if isinstance(exc, ProviderRequestBudgetExhausted):
        return LaneEvent.PROVIDER_REQUEST_BUDGET_REACHED, str(exc)
    if isinstance(exc, CircuitOpenError):
        return LaneEvent.CIRCUIT_OPEN, str(exc)
    if isinstance(exc, ProviderError):
        return LaneEvent.RETRY_EXHAUSTED, str(exc)
    if isinstance(exc, CursorConflictError):
        return LaneEvent.CURSOR_CONFLICT, str(exc)
    if isinstance(exc, JournalCorruptError):
        return LaneEvent.JOURNAL_CORRUPTION, str(exc)
    if isinstance(exc, KeyboardInterrupt):
        return LaneEvent.USER_INTERRUPTED, str(exc)
    return LaneEvent.LOCAL_CONSISTENCY_ERROR, f"{type(exc).__name__}: {exc}"


def _safe_error(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:500]


def _response_metadata_from_page(page: Any) -> ProviderResponseMetadata:
    """Read real response metadata attached by the typed page fetcher.

    The production adapters attach this object from ``RequestOutcome``.  A
    successful page without it is structurally incomplete and therefore
    repair-required; the executor must never fabricate a successful response.
    """
    value = getattr(page, "response_metadata", None)
    if isinstance(value, ProviderResponseMetadata):
        return value
    if isinstance(value, Mapping):
        return ProviderResponseMetadata.from_dict_strict(value)
    raise JournalCorruptError("successful provider page lacks real response_metadata")


def _build_exhaustion_evidence(
    provider: str,
    query_id: str,
    request_signature_hash: str,
    generation: int,
    cursor_before: str,
    metadata: ProviderResponseMetadata,
) -> ExhaustionEvidence:
    """Create evidence from a successful provider page without fake status."""
    return ExhaustionEvidence(
        provider=provider,
        query_id=query_id,
        request_signature=request_signature_hash,
        generation=generation,
        cursor_before=cursor_before,
        response_metadata=metadata,
        observed_at=metadata.observed_at,
    )


def _outcome(
    machine: LaneMachine,
    counters: LaneCounters,
    errors: list[LaneError],
    exhaustion_evidence: ExhaustionEvidence | None = None,
) -> LaneOutcome:
    """Build an outcome only after the lane has reached a legal terminal."""
    if not machine.terminal:
        machine.transition(LaneEvent.LOCAL_CONSISTENCY_ERROR)
        errors.append(LaneError(category="internal", message="lane ended without terminal state"))
    if machine.state != LaneState.EXHAUSTED:
        exhaustion_evidence = None
    return LaneOutcome(
        key=machine.lane_key,
        state=machine.state,
        stop_reason=machine.stop_reason or StopReason.LOCAL_CONSISTENCY_ERROR,
        counters=counters,
        exhaustion_evidence=exhaustion_evidence,
        errors=tuple(errors),
    )


def _populate_telemetry_delta(
    counters: LaneCounters,
    runtime: DiscoveryBatchRuntime,
    stable_lane_id: str,
    telemetry_before: dict[str, int],
) -> None:
    """Populate LaneCounters provider_requests_* from telemetry delta.

    Replaces manual ``counters.provider_requests_failed += 1`` with the
    actual delta observed by ProviderTelemetry across the lane's lifetime.
    """
    telemetry_after = runtime.telemetry.snapshot_lane(stable_lane_id)
    # Add telemetry delta to existing counters (which may already have
    # manual increments from failed pages, cursor conflicts, etc.)
    counters.provider_requests_attempted += max(
        0, telemetry_after.get("attempted", 0) - telemetry_before.get("attempted", 0)
    )
    counters.provider_requests_retried += max(
        0, telemetry_after.get("retried", 0) - telemetry_before.get("retried", 0)
    )
    counters.provider_requests_succeeded += max(
        0, telemetry_after.get("succeeded", 0) - telemetry_before.get("succeeded", 0)
    )
    counters.provider_requests_failed += max(
        0, telemetry_after.get("failed", 0) - telemetry_before.get("failed", 0)
    )


def _complete_refresh_lifecycle(
    service: RefreshStateService,
    spec: LaneExecutionSpec,
    *,
    counters: LaneCounters,
    errors: list[LaneError],
    page_ids: list[str],
    state: LaneState,
    stop_reason: StopReason | None,
) -> bool:
    """Persist the refresh window outcome on every executor exit path.

    The caller deliberately invokes this while the lane is still ``RUNNING``.
    If the notebook write fails we can take the legal RUNNING ->
    REPAIR_REQUIRED transition rather than fabricating a clean terminal
    outcome after an unrecorded refresh window.
    """
    try:
        service.complete_refresh(
            spec,
            state=state,
            stop_reason=stop_reason,
            pages=counters.pages_durable,
            items=counters.items_returned,
            page_ids=page_ids,
            error="; ".join(item.message for item in errors) or None,
        )
        return True
    except Exception as exc:
        # The provider page may already be durable, but inability to record its
        # window closure is local consistency damage and must be reported.
        errors.append(LaneError(category="refresh_state", message=_safe_error(exc)))
        return False


def execute_refresh_lane(
    spec: LaneExecutionSpec,
    *,
    runtime: DiscoveryBatchRuntime,
    notebook: KeywordNotebookStore,
    journal: PageJournalStore,
    options: Any,
    page_fetcher: ProviderPageFetcher,
    refresh_state: RefreshStateService,
    candidate_budget_exhausted: CandidateBudgetExhausted | None = None,
    notify_staging: StagingNotifier | None = None,
    on_page_persisted: PagePersisted | None = None,
) -> LaneOutcome:
    """Execute exactly one refresh lane described by *spec*."""
    if spec.key.mode != "refresh":
        raise ValueError("execute_refresh_lane requires a refresh LaneExecutionSpec")

    counters = LaneCounters()
    errors: list[LaneError] = []
    page_ids: list[str] = []
    machine = LaneMachine(lane_key=spec.key)
    client: ProviderClient | None = None
    terminal_event: LaneEvent | None = None
    stable_lane_id = spec.key.stable_id()
    telemetry_before = runtime.telemetry.snapshot_lane(stable_lane_id)

    def stop(event: LaneEvent) -> None:
        nonlocal terminal_event
        # Every terminal decision is previewed from RUNNING and then sealed
        # only after the durable refresh lifecycle record succeeds.
        machine.preview(event)
        terminal_event = event

    try:
        machine.transition(LaneEvent.START)
        refresh_state.begin_refresh(spec)
        client = runtime.provider_client(spec.key.provider)
        cursor = INITIAL_CURSOR
        for sequence in range(int(options.refresh_pages)):
            if candidate_budget_exhausted is not None and candidate_budget_exhausted(spec.key.keyword_id):
                stop(LaneEvent.CANDIDATE_BACKPRESSURE)
                break
            try:
                page = page_fetcher.fetch(spec, cursor, client)
            except ProviderRequestBudgetExhausted:
                stop(LaneEvent.PROVIDER_REQUEST_BUDGET_REACHED)
                break
            except Exception as exc:
                event, message = _typed_exception_to_lane_event(exc)
                errors.append(LaneError(category="provider", message=message))
                stop(event)
                break

            counters.logical_pages_attempted += 1
            counters.pages_fetched += 1
            if page.status == "failed":
                counters.provider_requests_failed += 1
                errors.append(LaneError(
                    category="provider",
                    message=page.safe_error or page.error_type or "provider page failed",
                ))
                event = (
                    LaneEvent.PERMANENT_FAILURE
                    if page.failure_class == "terminal"
                    else LaneEvent.RETRY_EXHAUSTED
                )
                stop(event)
                break

            page_id = refresh_page_id(
                keyword_id=spec.key.keyword_id,
                query_id=spec.key.query_id,
                provider=spec.key.provider,
                request_signature_hash=spec.request_signature.hash,
                refresh_run_id=str(spec.refresh_run_id),
                page_sequence=sequence,
            )
            response_metadata = _response_metadata_from_page(page)
            exhaustion_evidence = (
                _build_exhaustion_evidence(
                    spec.key.provider,
                    spec.key.query_id,
                    spec.request_signature.hash,
                    spec.key.generation,
                    cursor,
                    response_metadata,
                )
                if page.exhausted else None
            )
            page_data = journal.make_page(
                page_id=page_id,
                keyword_id=spec.key.keyword_id,
                keyword_zh=spec.keyword_zh,
                query_id=spec.key.query_id,
                query=spec.query,
                query_language=spec.query_language,
                provider=spec.key.provider,
                lane="refresh",
                lane_key=spec.key,
                generation=spec.key.generation,
                request_signature_value=spec.request_signature.to_dict(),
                request_cursor=cursor,
                next_cursor=page.next_cursor,
                provider_exhausted=page.exhausted,
                response_metadata=response_metadata,
                exhaustion_evidence=exhaustion_evidence,
                candidates=page.candidates,
                relevance_profile_hash=spec.relevance_profile_hash,
                refresh_run_id=spec.refresh_run_id,
                page_sequence=sequence,
                state="fetched",
            )
            page_path = journal.write_page(page_data)
            page_ids.append(page_id)
            counters.pages_durable += 1
            counters.items_returned += int(page.returned_count)
            counters.candidates_observed += len(page.candidates)

            if on_page_persisted is not None:
                on_page_persisted(page_path, page_data)
            if notify_staging is not None:
                notify_staging(spec.key.keyword_id, len(page_data.get("candidates") or []))

            if page.exhausted or not page.next_cursor:
                break
            cursor = page.next_cursor
        if terminal_event is None:
            stop(LaneEvent.REFRESH_WINDOW_COMPLETE)
    except KeyboardInterrupt as exc:
        errors.append(LaneError(category="interrupt", message=_safe_error(exc)))
        stop(LaneEvent.USER_INTERRUPTED)
    except Exception as exc:
        errors.append(LaneError(category="internal", message=_safe_error(exc)))
        event, _ = _typed_exception_to_lane_event(exc)
        stop(event)
    finally:
        # ``START`` normally succeeds before any fallible work.  If it did
        # not, the only legal report is local repair; still attempt to close a
        # refresh window so the notebook records the failure rather than
        # silently retaining an open window.
        if terminal_event is None:
            terminal_event = LaneEvent.LOCAL_CONSISTENCY_ERROR
        try:
            planned_state, planned_reason = machine.preview(terminal_event)
        except Exception as exc:
            errors.append(LaneError(category="internal", message=_safe_error(exc)))
            planned_state, planned_reason = (
                LaneState.REPAIR_REQUIRED,
                StopReason.LOCAL_CONSISTENCY_ERROR,
            )
            terminal_event = LaneEvent.LOCAL_CONSISTENCY_ERROR
        lifecycle_written = _complete_refresh_lifecycle(
            refresh_state,
            spec,
            counters=counters,
            errors=errors,
            page_ids=page_ids,
            state=planned_state,
            stop_reason=planned_reason,
        )
        if lifecycle_written:
            machine.transition(terminal_event)
        else:
            machine.transition(LaneEvent.LOCAL_CONSISTENCY_ERROR)

    _populate_telemetry_delta(counters, runtime, stable_lane_id, telemetry_before)
    return _outcome(machine, counters, errors)


def execute_backfill_lane(
    spec: LaneExecutionSpec,
    *,
    runtime: DiscoveryBatchRuntime,
    notebook: KeywordNotebookStore,
    journal: PageJournalStore,
    locks_dir: Path,
    page_fetcher: ProviderPageFetcher,
    candidate_budget_exhausted: CandidateBudgetExhausted | None = None,
    notify_staging: StagingNotifier | None = None,
    finalize_page: PageFinalizer | None = None,
) -> LaneOutcome:
    """Execute exactly one backfill lane described by *spec*."""
    if spec.key.mode != "backfill":
        raise ValueError("execute_backfill_lane requires a backfill LaneExecutionSpec")

    counters = LaneCounters()
    errors: list[LaneError] = []
    exhaustion_evidence: ExhaustionEvidence | None = None
    machine = LaneMachine(lane_key=spec.key)
    stable_lane_id = spec.key.stable_id()
    telemetry_before = runtime.telemetry.snapshot_lane(stable_lane_id)
    try:
        machine.transition(LaneEvent.START)
        client = runtime.provider_client(spec.key.provider)
        while not machine.terminal:
            if candidate_budget_exhausted is not None and candidate_budget_exhausted(spec.key.keyword_id):
                machine.transition(LaneEvent.CANDIDATE_BACKPRESSURE)
                break
            budget_result = runtime.page_budget.try_acquire(spec.key.stable_id())
            if budget_result == AcquireResult.BATCH_LIMIT_REACHED:
                machine.transition(LaneEvent.BATCH_PAGE_BUDGET_REACHED)
                break
            if budget_result == AcquireResult.LANE_LIMIT_REACHED:
                machine.transition(LaneEvent.LANE_PAGE_BUDGET_REACHED)
                break

            try:
                result: BackfillTransactionResult = run_backfill_page_transaction(
                    spec,
                    notebook_store=notebook,
                    journal_store=journal,
                    locks_dir=locks_dir,
                    page_fetcher=page_fetcher,
                    client=client,
                    finalize_page=finalize_page,
                )
            except ProviderRequestBudgetExhausted:
                machine.transition(LaneEvent.PROVIDER_REQUEST_BUDGET_REACHED)
                break
            except Exception as exc:
                event, message = _typed_exception_to_lane_event(exc)
                errors.append(LaneError(category="internal", message=message))
                if event == LaneEvent.STATE_LOCK_TIMEOUT:
                    counters.local_retryable_failures += 1
                else:
                    counters.local_consistency_failures += 1
                machine.transition(event)
                break

            counters.logical_pages_attempted += result.pages_requested
            counters.pages_fetched += result.pages_requested
            counters.pages_recovered += result.pages_recovered
            counters.pages_durable += result.pages_persisted
            counters.pages_cursor_committed += result.pages_committed
            counters.items_returned += result.candidates_returned
            counters.candidates_observed += result.candidates_returned

            if result.status == "stopped":
                machine.transition(LaneEvent.BATCH_PAGE_BUDGET_REACHED)
                break
            if result.status == "exhausted":
                exhaustion_evidence = getattr(result, "exhaustion_evidence", None)
                if exhaustion_evidence is None:
                    errors.append(LaneError(
                        category="journal",
                        message="exhausted backfill state lacks durable exhaustion evidence",
                    ))
                    machine.transition(LaneEvent.JOURNAL_CORRUPTION)
                else:
                    machine.transition(LaneEvent.PROVIDER_EXHAUSTED)
                break
            if result.status != "success":
                counters.provider_requests_failed += 1
                if result.safe_error:
                    errors.append(LaneError(category="provider", message=result.safe_error))
                if result.error_type in {"journal_corruption", "generation_drift"}:
                    counters.local_consistency_failures += 1
                    machine.transition(LaneEvent.JOURNAL_CORRUPTION)
                elif result.error_type == "cursor_conflict":
                    counters.cursor_conflicts += 1
                    machine.transition(LaneEvent.CURSOR_CONFLICT)
                elif result.error_type == "provider_terminal":
                    machine.transition(LaneEvent.PERMANENT_FAILURE)
                else:
                    machine.transition(LaneEvent.RETRY_EXHAUSTED)
                break
            if result.page_path is not None:
                try:
                    runtime.journal_index.add_page(
                        result.page_path,
                        journal.read(result.page_path),
                    )
                    runtime.metrics.journal_pages_written += int(result.pages_persisted > 0)
                    runtime.metrics.page_fsyncs += int(result.pages_persisted > 0)
                except Exception as exc:
                    errors.append(LaneError(category="journal", message=_safe_error(exc)))
                    counters.local_consistency_failures += 1
                    machine.transition(LaneEvent.JOURNAL_CORRUPTION)
                    break
            if notify_staging is not None and result.page_path is not None:
                notify_staging(spec.key.keyword_id, result.candidates_returned)
            if result.provider_exhausted:
                exhaustion_evidence = getattr(result, "exhaustion_evidence", None)
                if exhaustion_evidence is None:
                    errors.append(LaneError(
                        category="journal",
                        message="provider exhaustion is missing durable exhaustion evidence",
                    ))
                    machine.transition(LaneEvent.JOURNAL_CORRUPTION)
                else:
                    machine.transition(LaneEvent.PROVIDER_EXHAUSTED)
            else:
                # Successful non-exhausted page: lane is complete for this iteration.
                # The backfill transaction commits the cursor internally; we transition
                # to COMPLETED so the executor loop terminates cleanly.
                if not machine.terminal:
                    machine.transition(LaneEvent.BACKFILL_PAGE_COMPLETE)

    except Exception as exc:
        errors.append(LaneError(category="internal", message=_safe_error(exc)))
        if not machine.terminal:
            event, _ = _typed_exception_to_lane_event(exc)
            machine.transition(event)

    _populate_telemetry_delta(counters, runtime, stable_lane_id, telemetry_before)
    return _outcome(machine, counters, errors, exhaustion_evidence)
