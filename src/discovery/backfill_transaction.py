"""Journal-first Backfill page transaction.

The state execution lock serializes one keyword+query+provider backfill
state across processes while avoiding long-held notebook locks. Provider
requests happen under the state lock, but notebook file locks are held only for
short cursor reads / CAS commits.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from filelock import FileLock, Timeout

from src.discovery.constants import INITIAL_CURSOR
from src.discovery.keyword_notebook import (
    CursorConflictError,
    KeywordNotebookStore,
)
from src.discovery.page_journal import (
    PageJournalStore,
    backfill_page_id,
)


DISCOVERY_STATE_LOCK_TIMEOUT = 30


class StateLockTimeout(RuntimeError):
    """Raised when another process owns the same Backfill state too long."""


BackfillStopReason = Literal[
    "page_budget_exhausted",
    "provider_exhausted",
    "backpressure_active",
    "recovery_corruption",
]
BackfillErrorType = Literal[
    "provider_retryable",
    "provider_terminal",
    "journal_corruption",
    "cursor_conflict",
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
) -> BackfillTransactionResult:
    if stop_reason == "page_budget_exhausted" and error_type is not None:
        raise ValueError("page budget exhaustion must not carry error_type")
    if status == "success" and (safe_error or error_type):
        raise ValueError("successful Backfill transaction must not carry an error")
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
    )


def recover_last_committed_journal(
    *,
    keyword_zh: str,
    keyword_id: str,
    query_id: str,
    query: str,
    query_language: str,
    provider: str,
    request_signature_hash: str,
    notebook_store: KeywordNotebookStore,
    journal_store: PageJournalStore,
) -> BackfillTransactionResult | None:
    """Idempotently cross the journal cursor-commit boundary after a crash."""
    bf = notebook_store.get_backfill_state(keyword_zh, query_id, provider)
    page_id_value = str(bf.get("last_committed_page_id") or "")
    if not page_id_value:
        return None
    page_path = journal_store.page_path(
        keyword_id=keyword_id,
        query_id=query_id,
        provider=provider,
        lane="backfill",
        page_id=page_id_value,
    )
    if not page_path.exists():
        return _result(
            status="failed_retryable",
            page_path=None,
            page_id=page_id_value,
            safe_error=f"last committed journal missing: {page_id_value}",
            error_type="journal_corruption",
            stop_reason="recovery_corruption",
        )
    try:
        page_data = journal_store.read(page_path)
    except Exception as exc:
        return _result(
            status="failed_retryable",
            page_path=page_path,
            page_id=page_id_value,
            safe_error=str(exc),
            error_type="journal_corruption",
            stop_reason="recovery_corruption",
        )
    expected = {
        "keyword_id": keyword_id,
        "keyword_zh": keyword_zh,
        "query_id": query_id,
        "query": query,
        "query_language": query_language,
        "provider": provider,
        "lane": "backfill",
        "page_id": page_id_value,
    }
    for key, value in expected.items():
        if page_data.get(key) != value:
            return _result(
                status="failed_retryable",
                page_path=page_path,
                page_id=page_id_value,
                safe_error=f"last committed journal identity mismatch: {key}",
                error_type="journal_corruption",
                stop_reason="recovery_corruption",
            )
    if page_data.get("request_signature", {}).get("hash") != request_signature_hash:
        return _result(
            status="failed_retryable",
            page_path=page_path,
            page_id=page_id_value,
            safe_error="last committed journal request signature mismatch",
            error_type="journal_corruption",
            stop_reason="recovery_corruption",
        )
    state = str(page_data.get("state") or "")
    if state in {"cursor_committed", "draining", "drained"}:
        return None
    if state != "fetched":
        return _result(
            status="failed_retryable",
            page_path=page_path,
            page_id=page_id_value,
            safe_error=f"last committed journal has pre-commit/invalid state: {state}",
            error_type="journal_corruption",
            stop_reason="recovery_corruption",
        )
    current_cursor = str(bf.get("cursor") or INITIAL_CURSOR)
    next_cursor = page_data.get("next_cursor")
    ordinary_commit = next_cursor is not None and current_cursor == str(next_cursor)
    exhausted_commit = bool(bf.get("exhausted")) and bool(page_data.get("provider_exhausted"))
    if not (ordinary_commit or exhausted_commit):
        return _result(
            status="failed_retryable",
            page_path=page_path,
            page_id=page_id_value,
            safe_error="last committed journal disagrees with notebook cursor/exhausted state",
            error_type="journal_corruption",
            stop_reason="recovery_corruption",
        )
    page_data = journal_store.mark_cursor_committed(page_path)
    return _result(
        status="success",
        page_path=page_path,
        page_id=page_id_value,
        candidates_returned=int(page_data.get("statistics", {}).get("returned", 0)),
        provider_exhausted=bool(page_data.get("provider_exhausted")),
        pages_recovered=1,
        pages_committed=1,
        journals_recovered=1,
        stop_reason="provider_exhausted" if bool(page_data.get("provider_exhausted")) else None,
    )


def state_lock_path(
    locks_dir: Path,
    *,
    keyword_id: str,
    query_id: str,
    provider: str,
) -> Path:
    return Path(locks_dir) / keyword_id / query_id / f"{provider}.backfill.lock"


def run_backfill_page_transaction(
    *,
    keyword_zh: str,
    keyword_id: str,
    query_id: str,
    query: str,
    query_language: str,
    provider: str,
    notebook_store: KeywordNotebookStore,
    journal_store: PageJournalStore,
    locks_dir: Path,
    request_signature: dict[str, Any],
    page_size: int,
    fetch_page: Callable[..., Any],
    lock_timeout: int = DISCOVERY_STATE_LOCK_TIMEOUT,
) -> BackfillTransactionResult:
    """Fetch or recover one Backfill page and CAS-commit its cursor.

    ``fetch_page`` returns a ``DiscoveryPage``. It is called only when no
    existing fetched journal for the current cursor can be recovered.
    """
    lock_path = state_lock_path(
        locks_dir,
        keyword_id=keyword_id,
        query_id=query_id,
        provider=provider,
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_path), timeout=lock_timeout)
    try:
        with lock:
            notebook_store.ensure_backfill_generation(
                keyword_zh,
                query_id,
                provider,
                request_signature_hash=request_signature["hash"],
            )
            recovery = recover_last_committed_journal(
                keyword_zh=keyword_zh,
                keyword_id=keyword_id,
                query_id=query_id,
                query=query,
                query_language=query_language,
                provider=provider,
                request_signature_hash=request_signature["hash"],
                notebook_store=notebook_store,
                journal_store=journal_store,
            )
            if recovery is not None and recovery.status != "success":
                return recovery
            # Recovery stats must propagate into EVERY exit path below so the
            # coordinator report records that a journal was recovered this run,
            # even when the same transaction then stops/exhausts/fails. Notebook
            # totals are NOT re-incremented here — recovery only marks the page
            # cursor_committed; the totals were already advanced pre-crash.
            recovery_pages = recovery.pages_recovered if recovery is not None else 0
            recovery_journals = recovery.journals_recovered if recovery is not None else 0
            if notebook_store.is_backfill_exhausted(keyword_zh, query_id, provider):
                return _result(
                    status="exhausted",
                    page_path=None,
                    page_id="",
                    provider_exhausted=True,
                    pages_recovered=recovery_pages,
                    journals_recovered=recovery_journals,
                    stop_reason="provider_exhausted",
                )
            cursor = notebook_store.get_backfill_cursor(keyword_zh, query_id, provider) or INITIAL_CURSOR
            page_id_value = backfill_page_id(
                keyword_id=keyword_id,
                query_id=query_id,
                provider=provider,
                request_signature_hash=request_signature["hash"],
                request_cursor=cursor,
            )
            page_path = journal_store.page_path(
                keyword_id=keyword_id,
                query_id=query_id,
                provider=provider,
                lane="backfill",
                page_id=page_id_value,
            )
            pages_requested = 0
            pages_reused = 0
            pages_persisted = 0
            if page_path.exists():
                page_data = journal_store.read(page_path)
                pages_reused = 1
            else:
                page = fetch_page(
                    provider,
                    query,
                    keyword_zh=keyword_zh,
                    query_language=query_language,
                    lane="backfill",
                    page_size=page_size,
                    cursor=cursor,
                )
                pages_requested = 1
                if page.status == "failed":
                    if getattr(page, "error_type", None) == "page_budget_exhausted":
                        return _result(
                            status="stopped",
                            page_path=None,
                            page_id=page_id_value,
                            pages_requested=0,
                            pages_recovered=recovery_pages,
                            journals_recovered=recovery_journals,
                            stop_reason="page_budget_exhausted",
                        )
                    notebook_store.record_backfill_error(
                        keyword_zh,
                        query_id,
                        provider,
                        error=page.safe_error or page.error_type or "failed",
                    )
                    # Classify provider failure as terminal or retryable.
                    page_failure_class = getattr(page, "failure_class", None)
                    if page_failure_class == "terminal":
                        _error_type = "provider_terminal"
                        _status = "failed_terminal"
                    else:
                        _error_type = "provider_retryable"
                        _status = "failed_retryable"
                    return _result(
                        status=_status,
                        page_path=None,
                        page_id=page_id_value,
                        pages_requested=pages_requested,
                        pages_recovered=recovery_pages + pages_reused,
                        journals_recovered=recovery_journals,
                        safe_error=page.safe_error or page.error_type or "provider failed",
                        error_type=_error_type,
                    )
                generation = int(
                    notebook_store.get_backfill_state(
                        keyword_zh, query_id, provider,
                    ).get("generation") or 1
                )
                page_data = journal_store.make_page(
                    page_id=page_id_value,
                    keyword_id=keyword_id,
                    keyword_zh=keyword_zh,
                    query_id=query_id,
                    query=query,
                    query_language=query_language,
                    provider=provider,
                    lane="backfill",
                    generation=max(1, generation),
                    request_signature_value=request_signature,
                    request_cursor=cursor,
                    next_cursor=page.next_cursor,
                    provider_exhausted=page.exhausted,
                    candidates=page.candidates,
                    state="fetched",
                )
                page_path = journal_store.write_page(page_data)
                pages_persisted = 1
            if page_data["state"] == "fetched":
                try:
                    notebook_store.commit_backfill_cursor(
                        keyword_zh,
                        query_id,
                        provider,
                        expected_cursor=cursor,
                        next_cursor=page_data.get("next_cursor"),
                        committed_page_id=page_id_value,
                        exhausted=bool(page_data.get("provider_exhausted")),
                        items_this_page=int(page_data.get("statistics", {}).get("returned", 0)),
                    )
                except CursorConflictError as exc:
                    return _result(
                        status="failed_retryable",
                        page_path=page_path,
                        page_id=page_id_value,
                        pages_requested=pages_requested,
                        pages_recovered=recovery_pages + pages_reused,
                        pages_persisted=pages_persisted,
                        journals_recovered=recovery_journals,
                        safe_error=str(exc),
                        error_type="cursor_conflict",
                    )
                page_data = journal_store.mark_cursor_committed(page_path)
                pages_committed = 1
            else:
                pages_committed = 1 if page_data["state"] in {"cursor_committed", "draining", "drained"} else 0
            return _result(
                status="success",
                page_path=page_path,
                page_id=page_id_value,
                candidates_returned=int(page_data.get("statistics", {}).get("returned", 0)),
                provider_exhausted=bool(page_data.get("provider_exhausted")),
                pages_requested=pages_requested,
                pages_recovered=recovery_pages + pages_reused,
                pages_persisted=pages_persisted,
                pages_committed=pages_committed,
                journals_recovered=recovery_journals,
                stop_reason="provider_exhausted" if bool(page_data.get("provider_exhausted")) else None,
            )
    except Timeout as exc:
        raise StateLockTimeout(f"timed out waiting for Backfill state lock: {lock_path}") from exc
