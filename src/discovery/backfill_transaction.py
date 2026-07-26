"""Journal-first transaction for one immutable backfill lane.

The coordinator binds a backfill generation before submission.  Under the
state lock this transaction only validates that the frozen ``LaneExecutionSpec``
still matches durable notebook state; it never silently rebinds or rebuilds a
lane identity.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from filelock import FileLock, Timeout

from src.discovery.contracts.notebook import CursorConflictError
from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore
from src.discovery.execution.lane_models import (
    DurableProviderPage,
    LaneExecutionSpec,
)
from src.discovery.contracts.lane_history import (
    ExhaustionEvidence,
    ProviderResponseMetadata,
)
from src.discovery.contracts.page_journal import JournalCorruptError, backfill_page_id
from src.discovery.stores.page_journal_store import PageJournalStoreV4 as PageJournalStore
from src.discovery.providers.provider_client import ProviderClient
from src.discovery.providers.provider_errors import ProviderRequestBudgetExhausted
from src.discovery.providers.provider_page_fetcher import ProviderPageFetcher


DISCOVERY_STATE_LOCK_TIMEOUT = 30


class StateLockTimeout(RuntimeError):
    """Another process owns one physical backfill state too long."""


class StateLockTimeoutError(StateLockTimeout):
    """Named alias retained for callers that classify state-lock failures."""


BackfillStopReason = Literal["provider_exhausted", "recovery_corruption"]
BackfillErrorType = Literal[
    "provider_retryable",
    "provider_terminal",
    "journal_corruption",
    "cursor_conflict",
    "generation_drift",
]


@dataclass(frozen=True)
class BackfillTransactionResult:
    status: str
    page_path: Path | None
    page_id: str
    candidates_returned: int = 0
    provider_exhausted: bool = False
    pages_requested: int = 0
    pages_recovered: int = 0
    pages_persisted: int = 0
    pages_committed: int = 0
    journals_recovered: int = 0
    safe_error: str | None = None
    error_type: BackfillErrorType | None = None
    stop_reason: BackfillStopReason | None = None
    recovered_page_paths: tuple[Path, ...] = ()
    recovered_candidates_returned: int = 0
    exhaustion_evidence: ExhaustionEvidence | None = None


def _result(
    *,
    status: str,
    page_path: Path | None,
    page_id: str,
    candidates_returned: int = 0,
    provider_exhausted: bool = False,
    pages_requested: int = 0,
    pages_recovered: int = 0,
    pages_persisted: int = 0,
    pages_committed: int = 0,
    journals_recovered: int = 0,
    safe_error: str | None = None,
    error_type: BackfillErrorType | None = None,
    stop_reason: BackfillStopReason | None = None,
    recovered_page_paths: tuple[Path, ...] = (),
    recovered_candidates_returned: int = 0,
    exhaustion_evidence: ExhaustionEvidence | None = None,
) -> BackfillTransactionResult:
    if provider_exhausted and exhaustion_evidence is None:
        raise ValueError("exhausted BackfillTransactionResult requires durable evidence")
    if not provider_exhausted and exhaustion_evidence is not None:
        raise ValueError("non-exhausted BackfillTransactionResult must not carry evidence")
    return BackfillTransactionResult(
        status=status,
        page_path=page_path,
        page_id=page_id,
        candidates_returned=candidates_returned,
        provider_exhausted=provider_exhausted,
        pages_requested=pages_requested,
        pages_recovered=pages_recovered,
        pages_persisted=pages_persisted,
        pages_committed=pages_committed,
        journals_recovered=journals_recovered,
        safe_error=safe_error,
        error_type=error_type,
        stop_reason=stop_reason,
        recovered_page_paths=tuple(recovered_page_paths),
        recovered_candidates_returned=recovered_candidates_returned,
        exhaustion_evidence=exhaustion_evidence,
    )


def state_lock_path(locks_dir: Path, *, keyword_id: str, query_id: str, provider: str) -> Path:
    return Path(locks_dir) / keyword_id / query_id / f"{provider}.backfill.lock"


def _metadata_from_page(page: Any) -> ProviderResponseMetadata:
    value = getattr(page, "response_metadata", None)
    if isinstance(value, ProviderResponseMetadata):
        return value
    if isinstance(value, Mapping):
        return ProviderResponseMetadata.from_dict_strict(value)
    raise JournalCorruptError("successful provider page lacks real response_metadata")


def _evidence_for_page(spec: LaneExecutionSpec, cursor: str,
                       metadata: ProviderResponseMetadata) -> ExhaustionEvidence:
    return ExhaustionEvidence(
        provider=spec.key.provider,
        query_id=spec.key.query_id,
        request_signature=spec.request_signature.hash,
        generation=spec.key.generation,
        cursor_before=cursor,
        response_metadata=metadata,
        observed_at=metadata.observed_at,
    )


def _validate_durable_page(
    *,
    page_data: Mapping[str, Any],
    page_path: Path,
    journal_store: PageJournalStore,
    spec: LaneExecutionSpec,
    cursor: str | None = None,
) -> DurableProviderPage:
    durable = DurableProviderPage.from_journal(dict(page_data))
    expected_path = journal_store.page_path(
        keyword_id=spec.key.keyword_id,
        query_id=spec.key.query_id,
        provider=spec.key.provider,
        lane="backfill",
        page_id=durable.page_id,
    )
    if page_path.absolute() != expected_path.absolute():
        raise JournalCorruptError("durable backfill page path identity mismatch")
    if durable.lane_key != spec.key:
        raise JournalCorruptError("durable backfill lane_key drift")
    if page_data.get("request_signature") != spec.request_signature.to_dict():
        raise JournalCorruptError("durable backfill request signature drift")
    if cursor is not None and durable.cursor_before != cursor:
        raise JournalCorruptError("durable backfill cursor drift")
    if durable.provider_exhausted:
        evidence = durable.exhaustion_evidence
        if evidence is None:
            raise JournalCorruptError("exhausted durable page lacks evidence")
        if (
            evidence.provider != spec.key.provider
            or evidence.query_id != spec.key.query_id
            or evidence.request_signature != spec.request_signature.hash
            or evidence.generation != spec.key.generation
            or evidence.cursor_before != durable.cursor_before
            or evidence.response_metadata != durable.response_metadata
        ):
            raise JournalCorruptError("durable exhaustion evidence drift")
    elif durable.exhaustion_evidence is not None:
        raise JournalCorruptError("non-exhausted durable page carries evidence")
    return durable


def _state_evidence_matches(
    state: Mapping[str, Any], durable: DurableProviderPage,
) -> bool:
    if not durable.provider_exhausted or durable.exhaustion_evidence is None:
        return False
    return state.get("exhaustion_evidence") == durable.exhaustion_evidence.to_dict()


def recover_last_committed_journal(
    *,
    spec: LaneExecutionSpec,
    notebook_store: KeywordNotebookStore,
    journal_store: PageJournalStore,
    finalize_page: Callable[[Path], Mapping[str, Mapping[str, Any]]] | None = None,
) -> BackfillTransactionResult | None:
    """Finish the journal-to-cursor boundary using only durable evidence."""
    state = notebook_store.get_backfill_state(
        spec.keyword_zh, spec.key.query_id, spec.key.provider,
    )
    page_id = str(state.get("last_committed_page_id") or "")
    if not page_id:
        return None
    path = journal_store.page_path(
        keyword_id=spec.key.keyword_id,
        query_id=spec.key.query_id,
        provider=spec.key.provider,
        lane="backfill",
        page_id=page_id,
    )
    if not path.exists():
        return _result(
            status="failed_retryable", page_path=None, page_id=page_id,
            safe_error=f"last committed journal missing: {page_id}",
            error_type="journal_corruption", stop_reason="recovery_corruption",
        )
    try:
        page = journal_store.read(path)
        durable = _validate_durable_page(
            page_data=page, page_path=path, journal_store=journal_store, spec=spec,
        )
    except Exception as exc:
        return _result(
            status="failed_retryable", page_path=path, page_id=page_id,
            safe_error=f"last committed journal is invalid: {exc}",
            error_type="journal_corruption", stop_reason="recovery_corruption",
        )
    if page["state"] in {"cursor_committed", "draining", "drained"}:
        return None
    if page["state"] != "fetched":
        return _result(
            status="failed_retryable", page_path=path, page_id=page_id,
            safe_error=f"last committed journal has invalid state: {page['state']}",
            error_type="journal_corruption", stop_reason="recovery_corruption",
        )
    if durable.provider_exhausted:
        if not bool(state.get("exhausted")) or not _state_evidence_matches(state, durable):
            return _result(
                status="failed_retryable", page_path=path, page_id=page_id,
                safe_error="exhausted recovery state lacks matching durable evidence",
                error_type="journal_corruption", stop_reason="recovery_corruption",
            )
    elif str(state.get("cursor") or "") != str(durable.next_cursor or ""):
        return _result(
            status="failed_retryable", page_path=path, page_id=page_id,
            safe_error="cursor recovery state disagrees with durable page",
            error_type="journal_corruption", stop_reason="recovery_corruption",
        )
    if finalize_page is not None:
        try:
            page = journal_store.finalize_relevance(path, finalize_page(path))
        except ProviderRequestBudgetExhausted:
            raise
        except Exception as exc:
            return _result(
                status="failed_retryable", page_path=path, page_id=page_id,
                safe_error=f"relevance recovery failed: {type(exc).__name__}: {exc}",
                error_type="provider_retryable",
            )
    page = journal_store.mark_cursor_committed(path)
    return _result(
        status="success",
        page_path=path,
        page_id=page_id,
        candidates_returned=durable.returned_count,
        provider_exhausted=durable.provider_exhausted,
        pages_recovered=1,
        pages_committed=1,
        journals_recovered=1,
        stop_reason="provider_exhausted" if durable.provider_exhausted else None,
        exhaustion_evidence=durable.exhaustion_evidence,
    )


def _exhausted_state_result(
    *,
    spec: LaneExecutionSpec,
    state: Mapping[str, Any],
    journal_store: PageJournalStore,
) -> BackfillTransactionResult:
    page_id = str(state.get("last_committed_page_id") or "")
    if not page_id:
        return _result(
            status="failed_retryable", page_path=None, page_id="",
            safe_error="exhausted state has no durable committed page",
            error_type="journal_corruption", stop_reason="recovery_corruption",
        )
    path = journal_store.page_path(
        keyword_id=spec.key.keyword_id, query_id=spec.key.query_id,
        provider=spec.key.provider, lane="backfill", page_id=page_id,
    )
    try:
        durable = _validate_durable_page(
            page_data=journal_store.read(path), page_path=path,
            journal_store=journal_store, spec=spec,
        )
        if not _state_evidence_matches(state, durable):
            raise JournalCorruptError("exhausted state evidence does not match durable page")
        assert durable.exhaustion_evidence is not None
        return _result(
            status="exhausted", page_path=path, page_id=page_id,
            candidates_returned=durable.returned_count,
            provider_exhausted=True, stop_reason="provider_exhausted",
            exhaustion_evidence=durable.exhaustion_evidence,
        )
    except Exception as exc:
        return _result(
            status="failed_retryable", page_path=path, page_id=page_id,
            safe_error=f"exhausted state requires repair: {exc}",
            error_type="journal_corruption", stop_reason="recovery_corruption",
        )


def run_backfill_page_transaction(
    spec: LaneExecutionSpec,
    *,
    notebook_store: KeywordNotebookStore,
    journal_store: PageJournalStore,
    locks_dir: Path,
    page_fetcher: ProviderPageFetcher,
    client: ProviderClient,
    finalize_page: Callable[[Path], Mapping[str, Mapping[str, Any]]] | None = None,
    lock_timeout: int = DISCOVERY_STATE_LOCK_TIMEOUT,
) -> BackfillTransactionResult:
    """Fetch/recover and cursor-CAS one page for one exact backfill spec."""
    if spec.key.mode != "backfill":
        raise ValueError("backfill transaction requires a backfill LaneExecutionSpec")
    lock_path = state_lock_path(
        locks_dir,
        keyword_id=spec.key.keyword_id,
        query_id=spec.key.query_id,
        provider=spec.key.provider,
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(lock_path), timeout=lock_timeout):
            state = notebook_store.get_backfill_state(
                spec.keyword_zh, spec.key.query_id, spec.key.provider,
            )
            if (
                state.get("request_signature") != spec.request_signature.hash
                or int(state.get("generation") or 0) != spec.key.generation
            ):
                return _result(
                    status="failed_retryable", page_path=None, page_id="",
                    safe_error="backfill generation/signature drifted before transaction",
                    error_type="generation_drift", stop_reason="recovery_corruption",
                )

            recovery = recover_last_committed_journal(
                spec=spec,
                notebook_store=notebook_store,
                journal_store=journal_store,
                finalize_page=finalize_page,
            )
            if recovery is not None and recovery.status != "success":
                return recovery
            recovered = recovery or _result(status="success", page_path=None, page_id="")

            def carry_recovery(result: BackfillTransactionResult) -> BackfillTransactionResult:
                """Attach already-durable recovery facts to every later exit.

                A crash-recovered page remains a real recovery even if the
                next provider request, relevance finalization, or cursor CAS
                fails.  Dropping these counters makes reports non-conservative
                and invites a later run to claim progress was lost.
                """
                paths = tuple(
                    path for path in (*recovered.recovered_page_paths, recovered.page_path)
                    if path is not None
                )
                if result.page_path in paths:
                    result_paths = paths
                else:
                    result_paths = (*paths, *result.recovered_page_paths)
                return replace(
                    result,
                    pages_recovered=result.pages_recovered + recovered.pages_recovered,
                    pages_committed=result.pages_committed + recovered.pages_committed,
                    journals_recovered=result.journals_recovered + recovered.journals_recovered,
                    recovered_page_paths=result_paths,
                    recovered_candidates_returned=(
                        result.recovered_candidates_returned
                        + (
                            recovered.recovered_candidates_returned
                            or (
                                recovered.candidates_returned
                                if recovered.pages_recovered else 0
                            )
                        )
                    ),
                )
            if recovery is not None and recovery.provider_exhausted:
                return _result(
                    status="exhausted", page_path=recovery.page_path, page_id=recovery.page_id,
                    candidates_returned=recovery.candidates_returned,
                    provider_exhausted=True,
                    pages_recovered=recovery.pages_recovered,
                    pages_committed=recovery.pages_committed,
                    journals_recovered=recovery.journals_recovered,
                    recovered_page_paths=(recovery.page_path,) if recovery.page_path else (),
                    recovered_candidates_returned=recovery.candidates_returned,
                    stop_reason="provider_exhausted",
                    exhaustion_evidence=recovery.exhaustion_evidence,
                )

            state = notebook_store.get_backfill_state(
                spec.keyword_zh, spec.key.query_id, spec.key.provider,
            )
            if bool(state.get("exhausted")):
                exhausted = _exhausted_state_result(
                    spec=spec, state=state, journal_store=journal_store,
                )
                return carry_recovery(exhausted)

            cursor = str(state.get("cursor") or "*")
            page_id = backfill_page_id(
                keyword_id=spec.key.keyword_id,
                query_id=spec.key.query_id,
                provider=spec.key.provider,
                request_signature_hash=spec.request_signature.hash,
                request_cursor=cursor,
            )
            page_path = journal_store.page_path(
                keyword_id=spec.key.keyword_id, query_id=spec.key.query_id,
                provider=spec.key.provider, lane="backfill", page_id=page_id,
            )
            pages_requested = 0
            pages_persisted = 0
            reused_durable_page = False
            if page_path.exists():
                try:
                    page_data = journal_store.read(page_path)
                    durable = _validate_durable_page(
                        page_data=page_data, page_path=page_path,
                        journal_store=journal_store, spec=spec, cursor=cursor,
                    )
                    if page_data["state"] != "fetched":
                        raise JournalCorruptError("reused page is not fetched at current cursor")
                    reused_durable_page = True
                except Exception as exc:
                    return carry_recovery(_result(
                        status="failed_retryable", page_path=page_path, page_id=page_id,
                        safe_error=f"reused journal requires repair: {exc}",
                        error_type="journal_corruption", stop_reason="recovery_corruption",
                    ))
            else:
                page = page_fetcher.fetch(spec, cursor, client)
                pages_requested = 1
                if page.status == "failed":
                    error_type: BackfillErrorType = (
                        "provider_terminal" if page.failure_class == "terminal" else "provider_retryable"
                    )
                    notebook_store.record_backfill_error(
                        spec.keyword_zh, spec.key.query_id, spec.key.provider,
                        error=page.safe_error or page.error_type or "provider page failed",
                        error_type=page.error_type,
                        terminal=error_type == "provider_terminal",
                    )
                    return carry_recovery(_result(
                        status="failed_terminal" if error_type == "provider_terminal" else "failed_retryable",
                        page_path=None, page_id=page_id, pages_requested=1,
                        safe_error=page.safe_error or page.error_type or "provider page failed",
                        error_type=error_type,
                    ))
                try:
                    metadata = _metadata_from_page(page)
                    evidence = (
                        _evidence_for_page(spec, cursor, metadata)
                        if page.exhausted else None
                    )
                    page_data = journal_store.make_page(
                        page_id=page_id,
                        keyword_id=spec.key.keyword_id,
                        keyword_zh=spec.keyword_zh,
                        query_id=spec.key.query_id,
                        query=spec.query,
                        query_language=spec.query_language,
                        provider=spec.key.provider,
                        lane="backfill",
                        lane_key=spec.key,
                        generation=spec.key.generation,
                        request_signature_value=spec.request_signature.to_dict(),
                        request_cursor=cursor,
                        next_cursor=page.next_cursor,
                        provider_exhausted=page.exhausted,
                        response_metadata=metadata,
                        exhaustion_evidence=evidence,
                        candidates=page.candidates,
                        relevance_profile_hash=spec.relevance_profile_hash,
                        state="fetched",
                    )
                    page_path = journal_store.write_page(page_data)
                    durable = _validate_durable_page(
                        page_data=page_data, page_path=page_path,
                        journal_store=journal_store, spec=spec, cursor=cursor,
                    )
                    pages_persisted = 1
                except Exception as exc:
                    return carry_recovery(_result(
                        status="failed_retryable", page_path=None, page_id=page_id,
                        pages_requested=pages_requested,
                        safe_error=f"durable page persistence failed: {type(exc).__name__}: {exc}",
                        error_type="journal_corruption", stop_reason="recovery_corruption",
                    ))

            if page_data["state"] == "fetched" and finalize_page is not None:
                try:
                    page_data = journal_store.finalize_relevance(page_path, finalize_page(page_path))
                except ProviderRequestBudgetExhausted:
                    # A scope-verification request spent the shared batch
                    # valve. Preserve it for the executor's BUDGET_STOPPED
                    # mapping rather than recasting it as provider failure.
                    raise
                except Exception as exc:
                    return carry_recovery(_result(
                        status="failed_retryable", page_path=page_path, page_id=page_id,
                        pages_requested=pages_requested, pages_persisted=pages_persisted,
                        safe_error=f"relevance finalization failed: {type(exc).__name__}: {exc}",
                        error_type="provider_retryable",
                    ))
            try:
                notebook_store.commit_backfill_cursor(
                    spec.keyword_zh,
                    spec.key.query_id,
                    spec.key.provider,
                    expected_cursor=cursor,
                    next_cursor=durable.next_cursor,
                    committed_page_id=page_id,
                    exhausted=durable.provider_exhausted,
                    items_this_page=durable.returned_count,
                    exhaustion_evidence=(
                        None if durable.exhaustion_evidence is None
                        else durable.exhaustion_evidence.to_dict()
                    ),
                )
            except CursorConflictError as exc:
                return carry_recovery(_result(
                    status="failed_retryable", page_path=page_path, page_id=page_id,
                    pages_requested=pages_requested, pages_persisted=pages_persisted,
                    safe_error=str(exc), error_type="cursor_conflict",
                ))
            journal_store.mark_cursor_committed(page_path)
            return carry_recovery(_result(
                status="success",
                page_path=page_path,
                page_id=page_id,
                candidates_returned=durable.returned_count,
                provider_exhausted=durable.provider_exhausted,
                pages_requested=pages_requested,
                pages_recovered=int(reused_durable_page),
                pages_persisted=pages_persisted,
                pages_committed=1,
                journals_recovered=int(reused_durable_page),
                recovered_page_paths=(page_path,) if reused_durable_page else (),
                recovered_candidates_returned=(
                    durable.returned_count if reused_durable_page else 0
                ),
                stop_reason="provider_exhausted" if durable.provider_exhausted else None,
                exhaustion_evidence=durable.exhaustion_evidence,
            ))
    except Timeout as exc:
        raise StateLockTimeout(f"timed out waiting for Backfill state lock: {lock_path}") from exc
