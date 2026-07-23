"""Parametrized contract tests for the discovery lane state machine (v92).

Covers the full transition whitelist and asserts illegal transitions fail
closed (raise IllegalTransitionError) rather than being silently inferred.
Uses real LaneState/LaneEvent enums — no free-form strings.
"""
from __future__ import annotations

import pytest

from src.discovery.execution.lane_models import DiscoveryLaneKey, LaneState, StopReason
from src.discovery.execution.lane_state_machine import (
    ALLOWED_TRANSITIONS,
    EVENT_STOP_REASON,
    LaneEvent,
    IllegalTransitionError,
    LaneMachine,
    TERMINAL_STATES,
)

ALL_STATES = set(LaneState)
ALL_EVENTS = set(LaneEvent)


def _sample_key() -> DiscoveryLaneKey:
    from tests.helpers.fake_provider import lane_key_for_test
    return lane_key_for_test()


def _drive_to(machine: LaneMachine, target: LaneState) -> LaneMachine:
    """Drive a fresh machine to *target* along a known legal path."""
    paths: dict[LaneState, list[LaneEvent]] = {
        LaneState.READY: [],
        LaneState.RUNNING: [LaneEvent.START],
        LaneState.COMPLETED: [LaneEvent.START, LaneEvent.REFRESH_WINDOW_COMPLETE],
        LaneState.EXHAUSTED: [LaneEvent.START, LaneEvent.PROVIDER_EXHAUSTED],
        LaneState.BUDGET_STOPPED: [LaneEvent.START, LaneEvent.LANE_PAGE_BUDGET_REACHED],
        LaneState.RETRYABLE_FAILED: [LaneEvent.START, LaneEvent.RETRY_EXHAUSTED],
        LaneState.PERMANENT_FAILED: [LaneEvent.START, LaneEvent.PERMANENT_FAILURE],
        LaneState.REPAIR_REQUIRED: [LaneEvent.START, LaneEvent.LOCAL_CONSISTENCY_ERROR],
        LaneState.SKIPPED: [LaneEvent.SKIP_BY_MODE],
        LaneState.INTERRUPTED: [LaneEvent.START, LaneEvent.USER_INTERRUPTED],
    }
    for event in paths[target]:
        machine.transition(event)
    return machine


@pytest.mark.parametrize("from_state", sorted(ALL_STATES, key=lambda s: s.value))
@pytest.mark.parametrize("event", sorted(ALL_EVENTS, key=lambda e: e.value))
def test_transition_table(from_state: LaneState, event: LaneEvent) -> None:
    machine = _drive_to(LaneMachine(lane_key=_sample_key()), from_state)
    expected = ALLOWED_TRANSITIONS.get((from_state, event))
    if expected is None:
        with pytest.raises(IllegalTransitionError):
            machine.transition(event)
        assert machine.state == from_state
    else:
        assert machine.transition(event) == expected
        assert machine.state == expected


def test_exhausted_only_reachable_with_evidence_event() -> None:
    """Invariant: exhaustion reachable ONLY from running + provider_exhausted."""
    non_running = set(ALL_STATES) - {LaneState.RUNNING}
    for state in sorted(non_running, key=lambda s: s.value):
        machine = _drive_to(LaneMachine(lane_key=_sample_key()), state)
        if machine.terminal:
            continue
        with pytest.raises(IllegalTransitionError):
            machine.transition(LaneEvent.PROVIDER_EXHAUSTED)


def test_failure_and_budget_paths_never_reach_exhausted() -> None:
    """budget_stopped / retryable_failed / permanent_failed can never become
    exhausted (exhaustion requires provider evidence from running)."""
    for start_path in (
        [LaneEvent.START, LaneEvent.LANE_PAGE_BUDGET_REACHED],
        [LaneEvent.START, LaneEvent.RETRY_EXHAUSTED],
        [LaneEvent.START, LaneEvent.PERMANENT_FAILURE],
    ):
        machine = LaneMachine(lane_key=_sample_key())
        for event in start_path:
            machine.transition(event)
        with pytest.raises(IllegalTransitionError):
            machine.transition(LaneEvent.PROVIDER_EXHAUSTED)


def test_skipped_never_reaches_completed_or_exhausted() -> None:
    """A mode-skipped lane (never started) cannot be completed or exhausted."""
    machine = LaneMachine(lane_key=_sample_key())
    machine.transition(LaneEvent.SKIP_BY_MODE)
    assert machine.state == LaneState.SKIPPED
    for event in (LaneEvent.REFRESH_WINDOW_COMPLETE, LaneEvent.PROVIDER_EXHAUSTED,
                  LaneEvent.LANE_PAGE_BUDGET_REACHED):
        with pytest.raises(IllegalTransitionError):
            machine.transition(event)


def test_terminal_states_have_no_outgoing_transitions() -> None:
    for state in sorted(TERMINAL_STATES, key=lambda s: s.value):
        for event in sorted(ALL_EVENTS, key=lambda e: e.value):
            assert (state, event) not in ALLOWED_TRANSITIONS, (
                f"terminal state {state} must not allow event {event}"
            )


def test_stop_reason_recorded_on_terminal_transition() -> None:
    """The stop_reason is recorded together with the terminal transition."""
    cases = [
        (LaneEvent.SKIP_BY_MODE, LaneState.SKIPPED, StopReason.SKIPPED_BY_MODE),
        (LaneEvent.REFRESH_WINDOW_COMPLETE, LaneState.COMPLETED, StopReason.REFRESH_WINDOW_COMPLETE),
        (LaneEvent.PROVIDER_EXHAUSTED, LaneState.EXHAUSTED, StopReason.PROVIDER_EXHAUSTED),
        (LaneEvent.LANE_PAGE_BUDGET_REACHED, LaneState.BUDGET_STOPPED, StopReason.LANE_PAGE_BUDGET_REACHED),
        (LaneEvent.RETRY_EXHAUSTED, LaneState.RETRYABLE_FAILED, StopReason.RETRY_EXHAUSTED),
        (LaneEvent.PERMANENT_FAILURE, LaneState.PERMANENT_FAILED, StopReason.PERMANENT_PROVIDER_ERROR),
        (LaneEvent.LOCAL_CONSISTENCY_ERROR, LaneState.REPAIR_REQUIRED, StopReason.LOCAL_CONSISTENCY_ERROR),
    ]
    for event, expected_state, expected_stop_reason in cases:
        machine = LaneMachine(lane_key=_sample_key())
        if event != LaneEvent.SKIP_BY_MODE:
            machine.transition(LaneEvent.START)
        machine.transition(event)
        assert machine.state == expected_state
        assert machine.stop_reason == expected_stop_reason
        assert machine.status == expected_state.value


def test_history_is_auditable() -> None:
    from tests.helpers.fake_provider import lane_key_for_test
    key = lane_key_for_test(keyword_id="k", query_id="q")
    machine = LaneMachine(lane_key=key)
    machine.transition(LaneEvent.START, at="t0")
    machine.transition(LaneEvent.PROVIDER_EXHAUSTED, at="t1")
    assert [(t.from_state.value, t.event.value, t.to_state.value) for t in machine.history] == [
        ("ready", "start", "running"),
        ("running", "provider_exhausted", "exhausted"),
    ]
    assert machine.exhausted
    assert machine.terminal
    assert machine.stop_reason == StopReason.PROVIDER_EXHAUSTED
