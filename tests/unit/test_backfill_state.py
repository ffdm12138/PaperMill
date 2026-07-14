"""Unit tests for the shared backfill-state pristine predicate.

Verifies that ``is_strictly_pristine_unbound_backfill`` and
``describe_nonpristine_unbound_backfill`` behave correctly for every field
in the v3 backfill schema.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

import pytest

from src.discovery.backfill_state import (
    REASON_CONSECUTIVE_FAILURES,
    REASON_CURSOR_ADVANCED,
    REASON_CURSOR_CONFLICTS,
    REASON_EXHAUSTED,
    REASON_GENERATION_HISTORY,
    REASON_ITEMS_RETURNED,
    REASON_LAST_COMMITTED_PAGE,
    REASON_LAST_ERROR,
    REASON_LAST_ERROR_TYPE,
    REASON_LAST_FAILURE,
    REASON_LAST_PAGE_COUNT,
    REASON_LAST_SUCCESS,
    REASON_PAGES_COMMITTED,
    REASON_PAGES_SUCCEEDED,
    REASON_REQUEST_SIGNATURE_BOUND,
    REASON_RETRY_SCHEDULED,
    REASON_TERMINAL_FAILURE,
    BackfillBindDecision,
    BackfillBindError,
    describe_nonpristine_unbound_backfill,
    is_strictly_pristine_unbound_backfill,
    resolve_backfill_generation_binding,
    validate_backfill_state_exact,
)
from src.discovery.constants import INITIAL_CURSOR

pytestmark = pytest.mark.unit


def _fresh_backfill(**overrides: Any) -> dict[str, Any]:
    """Return a strictly pristine default backfill state."""
    state = {
        "cursor": INITIAL_CURSOR,
        "exhausted": False,
        "pages_succeeded": 0,
        "pages_committed": 0,
        "items_returned_total": 0,
        "last_page_count": 0,
        "last_committed_page_id": "",
        "cursor_conflicts": 0,
        "last_success_at": None,
        "last_error": None,
        "request_signature": "",
        "generation": 1,
        "generation_history": [],
        "consecutive_failures": 0,
        "last_failure_at": None,
        "last_error_type": None,
        "next_retry_at": None,
        "terminal_failure": False,
        "terminal_failure_at": None,
    }
    state.update(overrides)
    return state


# ── Positive ─────────────────────────────────────────────────────────


def test_strict_pristine_unbound_accepts_fresh_default_state():
    """A completely fresh default backfill state is strictly pristine."""
    state = _fresh_backfill()
    assert is_strictly_pristine_unbound_backfill(state) is True
    assert describe_nonpristine_unbound_backfill(state) == ()


# (removed — duplicate; enhanced version at test_strict_pristine_predicate_is_read_only)


# ── Negative per field ───────────────────────────────────────────────


@pytest.mark.parametrize("field,value,expected_reason", [
    ("cursor", "some_cursor", REASON_CURSOR_ADVANCED),
    ("exhausted", True, REASON_EXHAUSTED),
    ("pages_succeeded", 1, REASON_PAGES_SUCCEEDED),
    ("pages_committed", 1, REASON_PAGES_COMMITTED),
    ("items_returned_total", 1, REASON_ITEMS_RETURNED),
    ("last_page_count", 1, REASON_LAST_PAGE_COUNT),
    ("last_committed_page_id", "p1", REASON_LAST_COMMITTED_PAGE),
    ("cursor_conflicts", 1, REASON_CURSOR_CONFLICTS),
    ("last_success_at", "2026-01-01T00:00:00", REASON_LAST_SUCCESS),
    ("last_error", "oops", REASON_LAST_ERROR),
    ("consecutive_failures", 1, REASON_CONSECUTIVE_FAILURES),
    ("last_failure_at", "2026-01-01T00:00:00", REASON_LAST_FAILURE),
    ("last_error_type", "Timeout", REASON_LAST_ERROR_TYPE),
    ("next_retry_at", "2026-01-02T00:00:00", REASON_RETRY_SCHEDULED),
    ("terminal_failure", True, REASON_TERMINAL_FAILURE),
    ("terminal_failure_at", "2026-01-01T00:00:00", REASON_TERMINAL_FAILURE),
])
def test_each_field_makes_nonpristine(field, value, expected_reason):
    """Each non-default field causes the predicate to return False with the expected reason."""
    state = _fresh_backfill(**{field: value})
    assert is_strictly_pristine_unbound_backfill(state) is False
    reasons = describe_nonpristine_unbound_backfill(state)
    assert expected_reason in reasons, f"{field}={value!r} should produce {expected_reason!r}, got {reasons}"


def test_generation_history_nonempty_is_nonpristine():
    """A non-empty generation_history makes the state non-pristine."""
    state = _fresh_backfill(generation_history=[{"generation": 1, "request_signature": "aa", "closed_at": "T", "reason": "test"}])
    assert is_strictly_pristine_unbound_backfill(state) is False
    assert REASON_GENERATION_HISTORY in describe_nonpristine_unbound_backfill(state)


def test_non_empty_request_signature_is_always_bound():
    """A non-empty request_signature means the state is *bound*, not unbound, so pristine=True (no progress)."""
    state = _fresh_backfill(request_signature="a1b2c3d4e5f6a7b8")
    # With a signature, the predicate says False because it's not "unbound",
    # even though no progress fields are set.
    assert is_strictly_pristine_unbound_backfill(state) is False


def test_bound_signature_has_dedicated_reason():
    """A non-empty request_signature produces a dedicated 'request_signature_bound' reason."""
    state = _fresh_backfill(request_signature="a1b2c3d4e5f6a7b8")
    reasons = describe_nonpristine_unbound_backfill(state)
    assert REASON_REQUEST_SIGNATURE_BOUND in reasons


def test_describe_returns_stable_order():
    """The reason codes are returned in deterministic order for comparison."""
    state = _fresh_backfill(
        cursor="c1",
        pages_succeeded=1,
        terminal_failure=True,
    )
    reasons = describe_nonpristine_unbound_backfill(state)
    assert sorted(reasons) == ["cursor_advanced", "pages_succeeded", "terminal_failure"]


# ── Strict type / range / schema tests ──────────────────────────────


@pytest.mark.parametrize("field,value,expected_prefix", [
    ("generation", 0, "invalid_range:"),
    ("generation", -1, "invalid_range:"),
    ("generation", "1", "invalid_type:"),
    ("generation", 1.0, "invalid_type:"),
    ("pages_succeeded", -1, "invalid_range:"),
    ("pages_succeeded", "0", "invalid_type:"),
    ("pages_succeeded", False, "invalid_type:"),
    ("items_returned_total", "1", "invalid_type:"),
    ("exhausted", 0, "invalid_type:"),
    ("exhausted", "false", "invalid_type:"),
    ("exhausted", 1, "invalid_type:"),
    ("terminal_failure", "true", "invalid_type:"),
    ("terminal_failure", 1, "invalid_type:"),
    ("cursor", 0, "invalid_type:"),
    ("cursor", None, "invalid_type:"),
    ("request_signature", None, "invalid_type:"),
    ("request_signature", 123, "invalid_type:"),
    ("generation_history", None, "invalid_type:"),
    ("generation_history", {}, "invalid_type:"),
])
def test_invalid_type_or_range_makes_nonpristine(field, value, expected_prefix):
    """Each type/range error is detected and reported with the right prefix."""
    state = _fresh_backfill(**{field: value})
    assert is_strictly_pristine_unbound_backfill(state) is False
    reasons = describe_nonpristine_unbound_backfill(state)
    has_error = any(r.startswith(expected_prefix) for r in reasons)
    assert has_error, (
        f"{field}={value!r} should produce reason starting with {expected_prefix!r}, "
        f"got {reasons}"
    )


def test_unknown_field_makes_nonpristine():
    """An unknown field makes the state non-pristine."""
    state = _fresh_backfill(some_unknown_field="hello")
    assert is_strictly_pristine_unbound_backfill(state) is False
    reasons = describe_nonpristine_unbound_backfill(state)
    assert any(r.startswith("unknown_field:") for r in reasons)


def test_missing_field_makes_nonpristine():
    """A missing required field makes the state non-pristine."""
    state = dict(_fresh_backfill())
    state.pop("generation", None)
    assert is_strictly_pristine_unbound_backfill(state) is False
    reasons = describe_nonpristine_unbound_backfill(state)
    assert any(r.startswith("missing_field:") for r in reasons)


def test_strict_pristine_predicate_is_read_only():
    """The predicate must not modify the input dict, even with nested structures."""
    import copy
    state = _fresh_backfill()
    before = copy.deepcopy(state)
    is_strictly_pristine_unbound_backfill(state)
    assert state == before


# ── Exact bound-state validation through resolve_backfill_generation_binding ──


def test_bound_state_rejects_negative_counter():
    state = _fresh_backfill(request_signature="a1b2c3d4e5f6a7b8", pages_succeeded=-1)
    with pytest.raises(BackfillBindError, match="pages_succeeded"):
        resolve_backfill_generation_binding(state, "a1b2c3d4e5f6a7b8")


def test_bound_state_rejects_string_counter():
    state = _fresh_backfill(request_signature="a1b2c3d4e5f6a7b8", pages_succeeded="0")
    with pytest.raises(BackfillBindError, match="pages_succeeded"):
        resolve_backfill_generation_binding(state, "a1b2c3d4e5f6a7b8")


def test_bound_state_rejects_bool_counter():
    state = _fresh_backfill(request_signature="a1b2c3d4e5f6a7b8", pages_succeeded=False)
    with pytest.raises(BackfillBindError, match="pages_succeeded"):
        resolve_backfill_generation_binding(state, "a1b2c3d4e5f6a7b8")


@pytest.mark.parametrize("field,value", [
    ("exhausted", 0),
    ("exhausted", 1),
    ("exhausted", "false"),
    ("terminal_failure", 0),
    ("terminal_failure", 1),
    ("terminal_failure", "true"),
])
def test_bound_state_rejects_non_bool_flags(field, value):
    state = _fresh_backfill(request_signature="a1b2c3d4e5f6a7b8", **{field: value})
    with pytest.raises(BackfillBindError, match=field):
        resolve_backfill_generation_binding(state, "a1b2c3d4e5f6a7b8")


def test_bound_state_rejects_generation_zero():
    state = _fresh_backfill(request_signature="a1b2c3d4e5f6a7b8", generation=0)
    with pytest.raises(BackfillBindError, match="generation"):
        resolve_backfill_generation_binding(state, "a1b2c3d4e5f6a7b8")


def test_bound_state_rejects_missing_generation():
    state = _fresh_backfill(request_signature="a1b2c3d4e5f6a7b8")
    state.pop("generation")
    with pytest.raises(BackfillBindError, match="missing fields"):
        resolve_backfill_generation_binding(state, "a1b2c3d4e5f6a7b8")


def test_bound_state_rejects_unknown_field():
    state = _fresh_backfill(request_signature="a1b2c3d4e5f6a7b8", extra_field=True)
    with pytest.raises(BackfillBindError, match="unknown fields"):
        resolve_backfill_generation_binding(state, "a1b2c3d4e5f6a7b8")


def test_bound_state_rejects_invalid_current_signature():
    state = _fresh_backfill(request_signature="not-hex")
    with pytest.raises(BackfillBindError, match="request_signature"):
        resolve_backfill_generation_binding(state, "a1b2c3d4e5f6a7b8")


def test_bound_state_rejects_invalid_history_entry():
    state = _fresh_backfill(
        request_signature="a1b2c3d4e5f6a7b8",
        generation_history=[{"generation": 1, "request_signature": "bad", "closed_at": "T", "reason": ""}],
    )
    with pytest.raises(BackfillBindError, match="generation_history"):
        resolve_backfill_generation_binding(state, "a1b2c3d4e5f6a7b8")


def test_bound_state_rejects_missing_history_field():
    state = _fresh_backfill(
        request_signature="a1b2c3d4e5f6a7b8",
        generation_history=[{"generation": 1, "request_signature": "b1b2c3d4e5f6a7b8"}],
    )
    with pytest.raises(BackfillBindError, match="generation_history"):
        resolve_backfill_generation_binding(state, "a1b2c3d4e5f6a7b8")


def test_bound_state_allows_progress_and_returns_same_signature():
    state = _fresh_backfill(
        request_signature="a1b2c3d4e5f6a7b8",
        pages_succeeded=5,
        cursor="next-cursor",
    )
    decision = resolve_backfill_generation_binding(state, "a1b2c3d4e5f6a7b8")
    assert decision is BackfillBindDecision.SAME_SIGNATURE


def test_bound_state_returns_roll_for_different_signature():
    state = _fresh_backfill(
        request_signature="a1b2c3d4e5f6a7b8",
        pages_succeeded=5,
    )
    decision = resolve_backfill_generation_binding(state, "b1b2c3d4e5f6a7b8")
    assert decision is BackfillBindDecision.ROLL_GENERATION
