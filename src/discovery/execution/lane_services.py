"""Typed service container for lane execution.

Extracted from coordinator nested closures so that lane executors depend
on interface methods rather than on coordinator-internal state.
"""
from __future__ import annotations

from typing import Any

from src.discovery.execution.lane_models import LaneExecutionSpec, LaneState, StopReason


class RefreshStateService:
    """Concrete durable refresh-window lifecycle service.

    ``KeywordNotebookStore`` owns the only writable refresh state.  The lane
    executor invokes this service in ``finally`` so a window has a durable
    start and terminal record even when fetching or finalization fails.
    """

    def __init__(self, notebook: Any) -> None:
        self._notebook = notebook

    def begin_refresh(self, spec: LaneExecutionSpec) -> None:
        self._notebook.begin_refresh(
            spec.keyword_zh,
            spec.key.query_id,
            spec.key.provider,
        )

    def complete_refresh(
        self,
        spec: LaneExecutionSpec,
        *,
        state: LaneState,
        stop_reason: StopReason | None,
        pages: int,
        items: int,
        page_ids: list[str],
        error: str | None,
    ) -> None:
        clean = {
            LaneState.COMPLETED,
            LaneState.EXHAUSTED,
            LaneState.BUDGET_STOPPED,
        }
        # Notebook refresh state deliberately has a smaller persisted status
        # vocabulary than the lane machine.  Never write a LaneState value
        # (for example ``permanent_failed``) into it: that would fail its
        # schema validation and convert a genuine provider failure into a
        # secondary local repair failure.  The precise terminal state remains
        # in ``LaneOutcome``/the batch report; the notebook records the
        # durable coarse lifecycle status plus its stop-reason detail.
        status = "success" if state in clean else "failed"
        detail = error or (
            None if stop_reason is None else stop_reason.value
        )
        self._notebook.complete_refresh(
            spec.keyword_zh,
            spec.key.query_id,
            spec.key.provider,
            status=status,
            pages_scanned=pages,
            items_returned=items,
            error=detail,
            window_signature=spec.request_signature.hash,
            window_pages=pages,
            window_page_ids=list(page_ids),
        )
