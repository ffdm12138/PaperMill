"""Explicit lane state machine for DOI discovery lanes (v92 production model).

One machine exists per physical lane (keyword × query × provider × mode).
The machine is in-memory only - the durable truth remains the notebook and the
page journal - but every status reported to the batch report and every cursor /
exhaustion write is gated through this machine so illegal transitions fail
closed instead of being inferred from ad-hoc flag combinations.

This is the **single** production state entry point: the executor drives
events, the machine transitions to a terminal state, and the lane outcome's
``state`` / ``stop_reason`` are derived from the terminal state - never set
inline by heuristic flag combinations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar

from src.discovery.execution.lane_models import DiscoveryLaneKey, LaneState, StopReason


class LaneEvent(str, Enum):
    """Every event that can drive a lane state transition.

    No module may use free-form strings for lane events.
    """
    START = "start"
    SKIP_BY_MODE = "skip_by_mode"
    SKIP_BY_BACKPRESSURE = "skip_by_backpressure"
    REFRESH_WINDOW_COMPLETE = "refresh_window_complete"
    PROVIDER_EXHAUSTED = "provider_exhausted"
    LANE_PAGE_BUDGET_REACHED = "lane_page_budget_reached"
    BATCH_PAGE_BUDGET_REACHED = "batch_page_budget_reached"
    PROVIDER_REQUEST_BUDGET_REACHED = "provider_request_budget_reached"
    CANDIDATE_BACKPRESSURE = "candidate_backpressure"
    RETRY_EXHAUSTED = "retry_exhausted"
    STATE_LOCK_TIMEOUT = "state_lock_timeout"
    CIRCUIT_OPEN = "circuit_open"
    PERMANENT_FAILURE = "permanent_failure"
    CURSOR_CONFLICT = "cursor_conflict"
    JOURNAL_CORRUPTION = "journal_corruption"
    LOCAL_CONSISTENCY_ERROR = "local_consistency_error"
    USER_INTERRUPTED = "user_interrupted"
    BACKFILL_PAGE_COMPLETE = "backfill_page_complete"


#: Terminal states (ready/running are the only non-terminal states).
TERMINAL_STATES: frozenset[LaneState] = frozenset({
    LaneState.COMPLETED, LaneState.EXHAUSTED, LaneState.BUDGET_STOPPED,
    LaneState.RETRYABLE_FAILED, LaneState.PERMANENT_FAILED,
    LaneState.REPAIR_REQUIRED, LaneState.SKIPPED, LaneState.INTERRUPTED,
})

#: Clean terminal states that count as a successful lane ending.
CLEAN_TERMINAL_STATES: frozenset[LaneState] = frozenset({
    LaneState.COMPLETED, LaneState.EXHAUSTED, LaneState.BUDGET_STOPPED,
})

#: Failure terminal states.
FAILURE_TERMINAL_STATES: frozenset[LaneState] = frozenset({
    LaneState.RETRYABLE_FAILED, LaneState.PERMANENT_FAILED,
})

#: Explicit transition whitelist: (from_state, event) -> to_state.
ALLOWED_TRANSITIONS: dict[tuple[LaneState, LaneEvent], LaneState] = {
    # Start
    (LaneState.READY, LaneEvent.START): LaneState.RUNNING,
    (LaneState.READY, LaneEvent.SKIP_BY_MODE): LaneState.SKIPPED,
    (LaneState.READY, LaneEvent.SKIP_BY_BACKPRESSURE): LaneState.SKIPPED,
    # Running -> clean endings
    (LaneState.RUNNING, LaneEvent.REFRESH_WINDOW_COMPLETE): LaneState.COMPLETED,
    (LaneState.RUNNING, LaneEvent.BACKFILL_PAGE_COMPLETE): LaneState.COMPLETED,
    (LaneState.RUNNING, LaneEvent.PROVIDER_EXHAUSTED): LaneState.EXHAUSTED,
    (LaneState.RUNNING, LaneEvent.LANE_PAGE_BUDGET_REACHED): LaneState.BUDGET_STOPPED,
    (LaneState.RUNNING, LaneEvent.BATCH_PAGE_BUDGET_REACHED): LaneState.BUDGET_STOPPED,
    (LaneState.RUNNING, LaneEvent.PROVIDER_REQUEST_BUDGET_REACHED): LaneState.BUDGET_STOPPED,
    (LaneState.RUNNING, LaneEvent.CANDIDATE_BACKPRESSURE): LaneState.BUDGET_STOPPED,
    # Running -> failure endings
    (LaneState.RUNNING, LaneEvent.RETRY_EXHAUSTED): LaneState.RETRYABLE_FAILED,
    (LaneState.RUNNING, LaneEvent.PERMANENT_FAILURE): LaneState.PERMANENT_FAILED,
    (LaneState.RUNNING, LaneEvent.STATE_LOCK_TIMEOUT): LaneState.RETRYABLE_FAILED,
    (LaneState.RUNNING, LaneEvent.CIRCUIT_OPEN): LaneState.RETRYABLE_FAILED,
    (LaneState.RUNNING, LaneEvent.CURSOR_CONFLICT): LaneState.REPAIR_REQUIRED,
    (LaneState.RUNNING, LaneEvent.JOURNAL_CORRUPTION): LaneState.REPAIR_REQUIRED,
    (LaneState.RUNNING, LaneEvent.LOCAL_CONSISTENCY_ERROR): LaneState.REPAIR_REQUIRED,
    (LaneState.RUNNING, LaneEvent.USER_INTERRUPTED): LaneState.INTERRUPTED,
}

#: Event -> StopReason mapping (frozen report vocabulary).
EVENT_STOP_REASON: dict[LaneEvent, StopReason] = {
    LaneEvent.SKIP_BY_MODE: StopReason.SKIPPED_BY_MODE,
    LaneEvent.SKIP_BY_BACKPRESSURE: StopReason.CANDIDATE_BACKPRESSURE,
    LaneEvent.REFRESH_WINDOW_COMPLETE: StopReason.REFRESH_WINDOW_COMPLETE,
    LaneEvent.PROVIDER_EXHAUSTED: StopReason.PROVIDER_EXHAUSTED,
    LaneEvent.LANE_PAGE_BUDGET_REACHED: StopReason.LANE_PAGE_BUDGET_REACHED,
    LaneEvent.BATCH_PAGE_BUDGET_REACHED: StopReason.BATCH_PAGE_BUDGET_REACHED,
    LaneEvent.PROVIDER_REQUEST_BUDGET_REACHED: StopReason.PROVIDER_REQUEST_BUDGET_REACHED,
    LaneEvent.CANDIDATE_BACKPRESSURE: StopReason.CANDIDATE_BACKPRESSURE,
    LaneEvent.RETRY_EXHAUSTED: StopReason.RETRY_EXHAUSTED,
    LaneEvent.STATE_LOCK_TIMEOUT: StopReason.STATE_LOCK_TIMEOUT,
    LaneEvent.CIRCUIT_OPEN: StopReason.CIRCUIT_OPEN,
    LaneEvent.PERMANENT_FAILURE: StopReason.PERMANENT_PROVIDER_ERROR,
    LaneEvent.CURSOR_CONFLICT: StopReason.CURSOR_CONFLICT,
    LaneEvent.JOURNAL_CORRUPTION: StopReason.JOURNAL_CORRUPTION,
    LaneEvent.LOCAL_CONSISTENCY_ERROR: StopReason.LOCAL_CONSISTENCY_ERROR,
    LaneEvent.USER_INTERRUPTED: StopReason.USER_INTERRUPTED,
}


class IllegalTransitionError(RuntimeError):
    """Raised when an event is not whitelisted for the current state."""

    def __init__(self, state: LaneState, event: LaneEvent) -> None:
        super().__init__(f"illegal lane transition: state={state!r} event={event!r}")
        self.state = state
        self.event = event


@dataclass(frozen=True)
class LaneTransition:
    from_state: LaneState
    event: LaneEvent
    to_state: LaneState
    at: str


@dataclass
class LaneMachine:
    """Per-lane state machine with an auditable transition history.

    One machine per physical lane key.  ``state`` and ``stop_reason`` are
    derived from the terminal state and the event that reached it — callers
    must never set them directly.
    """

    lane_key: DiscoveryLaneKey
    state: LaneState = LaneState.READY
    stop_reason: StopReason | None = None
    history: list[LaneTransition] = field(default_factory=list)

    def preview(self, event: LaneEvent) -> tuple[LaneState, StopReason | None]:
        """Validate an event and return its resulting state without mutating.

        Refresh lanes must durably close their refresh window before their
        terminal transition is sealed.  Keeping this preview in the state
        machine avoids a parallel, ad-hoc event-to-status mapping in the
        executor while preserving the invariant that terminal states have no
        outgoing transitions.
        """
        target = ALLOWED_TRANSITIONS.get((self.state, event))
        if target is None:
            raise IllegalTransitionError(self.state, event)
        return target, EVENT_STOP_REASON.get(event)

    def transition(self, event: LaneEvent, *, at: str = "") -> LaneState:
        target, stop_reason = self.preview(event)
        self.history.append(
            LaneTransition(from_state=self.state, event=event, to_state=target, at=at)
        )
        self.state = target
        if stop_reason is not None:
            self.stop_reason = stop_reason
        return target

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def exhausted(self) -> bool:
        return self.state == LaneState.EXHAUSTED

    @property
    def status(self) -> str:
        """Lane status for the report (== terminal state value).

        Pre-terminal lanes report ``"running"``; the coordinator only reads
        ``status`` after the lane has reached a terminal state.
        """
        return self.state.value
