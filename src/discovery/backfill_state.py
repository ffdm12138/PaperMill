"""Shared strict-pristine backfill state predicates.

This module defines the *single* authoritative ``is_strictly_pristine_unbound_backfill``
predicate and its companion ``describe_nonpristine_unbound_backfill``.  Every consumer
— schema validator, ``KeywordNotebookStore``, Audit, Migration, Recovery — **must**
use these helpers instead of inlining their own pristine check.

A backfill generation is *strictly pristine unbound* only when **every** field:

- has the **exact** expected Python type (``type(value) is int`` for counters,
  ``type(value) is bool`` for boolean flags, ``type(value) is str`` for text…);
- has a **legal** range (counters are exactly 0, generation >= 1, …);
- is in its default (never-touched) state;
- is the complete set of known fields with **no unknown** fields;
- and ``request_signature`` is the schema-defined empty value (``""``).

Note: this helper assesses only "has this generation never been bound to a
request contract?"  It is **not** a general-purpose validator for an already-
bound generation.  A non-empty ``request_signature`` immediately returns
``False`` (not unbound) without inspecting the remaining state fields.

Any deviation means the generation has real work or a failure that cannot be
silently attributed to a new request contract.
"""

from __future__ import annotations

from collections.abc import Mapping, Set
import re
from typing import Any

from src.discovery.constants import (
    BACKFILL_STATE_ACCEPTED_FIELDS,
    BACKFILL_STATE_FIELDS,
    INITIAL_CURSOR,
)


# Regex for valid 16-hex signature.
_HEX16 = re.compile(r"^[0-9a-f]{16}$")


# ── Stable reason codes ──────────────────────────────────────────────

# State-level reasons
REASON_GENERATION_HISTORY = "generation_history"
REASON_CURSOR_ADVANCED = "cursor_advanced"
REASON_EXHAUSTED = "exhausted"
REASON_PAGES_SUCCEEDED = "pages_succeeded"
REASON_PAGES_COMMITTED = "pages_committed"
REASON_ITEMS_RETURNED = "items_returned"
REASON_LAST_PAGE_COUNT = "last_page_count"
REASON_LAST_COMMITTED_PAGE = "last_committed_page"
REASON_CURSOR_CONFLICTS = "cursor_conflicts"
REASON_LAST_SUCCESS = "last_success"
REASON_LAST_ERROR = "last_error"
REASON_CONSECUTIVE_FAILURES = "consecutive_failures"
REASON_LAST_FAILURE = "last_failure"
REASON_LAST_ERROR_TYPE = "last_error_type"
REASON_RETRY_SCHEDULED = "retry_scheduled"
REASON_TERMINAL_FAILURE = "terminal_failure"

# Signature-bound reason (separate from field-level codes)
REASON_REQUEST_SIGNATURE_BOUND = "request_signature_bound"

# Type / range / schema reasons
UNKNOWN_FIELD_PREFIX = "unknown_field:"
MISSING_FIELD_PREFIX = "missing_field:"
INVALID_TYPE_PREFIX = "invalid_type:"
INVALID_RANGE_PREFIX = "invalid_range:"


# ── Field classification ─────────────────────────────────────────────

_INT_FIELDS: Set[str] = frozenset({
    "pages_succeeded", "pages_committed", "items_returned_total",
    "last_page_count", "cursor_conflicts", "consecutive_failures",
})

_BOOL_FIELDS: Set[str] = frozenset({"exhausted", "terminal_failure"})

_NULLABLE_TEXT_FIELDS: Set[str] = frozenset({
    "last_committed_page_id",
    "last_success_at", "last_error", "last_failure_at", "last_error_type",
    "next_retry_at", "terminal_failure_at",
})

# Map field names to their stable non-pristine reason codes.
_FIELD_REASON: dict[str, str] = {
    "cursor": REASON_CURSOR_ADVANCED,
    "exhausted": REASON_EXHAUSTED,
    "pages_succeeded": REASON_PAGES_SUCCEEDED,
    "pages_committed": REASON_PAGES_COMMITTED,
    "items_returned_total": REASON_ITEMS_RETURNED,
    "last_page_count": REASON_LAST_PAGE_COUNT,
    "last_committed_page_id": REASON_LAST_COMMITTED_PAGE,
    "cursor_conflicts": REASON_CURSOR_CONFLICTS,
    "last_success_at": REASON_LAST_SUCCESS,
    "last_error": REASON_LAST_ERROR,
    "consecutive_failures": REASON_CONSECUTIVE_FAILURES,
    "last_failure_at": REASON_LAST_FAILURE,
    "last_error_type": REASON_LAST_ERROR_TYPE,
    "next_retry_at": REASON_RETRY_SCHEDULED,
    "terminal_failure": REASON_TERMINAL_FAILURE,
    "terminal_failure_at": REASON_TERMINAL_FAILURE,
}

# ── Public helpers ───────────────────────────────────────────────────


def is_strictly_pristine_unbound_backfill(
    state: Mapping[str, Any],
) -> bool:
    """Return ``True`` when *state* is a never-activated backfill generation.

    ``True`` means the generation is safe for a first-time ``request_signature``
    bind.  ``False`` means the state has some form of durable progress, failure,
    retry, terminal condition, history, unknown fields, missing fields, or type
    errors and **must never** be silently rebound.

    The predicate is a pure read — it never modifies *state*.
    """
    # Short-circuit: a non-empty signature always means bound, not unbound.
    sig = state.get("request_signature")
    if sig is not None and sig != "":
        return False
    return not _nonpristine_reasons(state)


def describe_nonpristine_unbound_backfill(
    state: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return the stable reason codes why *state* is not strictly pristine.

    An empty tuple means the state **is** strictly pristine unbound.
    Codes are stable across releases and returned in field-group order for
    deterministic comparison.
    """
    return tuple(_nonpristine_reasons(state))


# ── Internal ─────────────────────────────────────────────────────────


def _nonpristine_reasons(state: Mapping[str, Any]) -> list[str]:
    """Collect every non-pristine reason code for *state*."""
    reasons: list[str] = []

    # ── Exact field set ────────────────────────────────────────────

    state_keys: Set[str] = set(state)

    # Missing fields
    missing = sorted(BACKFILL_STATE_FIELDS - state_keys)
    for field in missing:
        reasons.append(f"{MISSING_FIELD_PREFIX}{field}")

    # Unknown fields (optional execution-contract extension fields accepted)
    unknown = sorted(state_keys - BACKFILL_STATE_ACCEPTED_FIELDS)
    for field in unknown:
        reasons.append(f"{UNKNOWN_FIELD_PREFIX}{field}")

    # ── Per-field type & value checks ──────────────────────────────
    # Only check fields that are present; missing fields are already reported.

    # Generation (int >= 1)
    _check_int_ge(state, reasons, "generation", 1)

    # Integer counters (type is int, value == 0)
    for field in sorted(_INT_FIELDS):
        _check_int_eq_zero(state, reasons, field)

    # Boolean flags (type is bool, value is False)
    for field in sorted(_BOOL_FIELDS):
        _check_bool_false(state, reasons, field)

    # Text / nullable fields
    _check_cursor(state, reasons)
    _check_request_signature(state, reasons)
    for field in sorted(_NULLABLE_TEXT_FIELDS):
        _check_nullable_text(state, reasons, field)

    # generation_history (type is list, value == [])
    _check_history(state, reasons)

    return reasons


# ── Per-field check helpers ──────────────────────────────────────────


def _check_int_ge(
    state: Mapping[str, Any],
    reasons: list[str],
    field: str,
    minimum: int,
) -> None:
    """Fail if *field* is not int or is < minimum."""
    value = state.get(field)
    if type(value) is not int:
        reasons.append(f"{INVALID_TYPE_PREFIX}{field}")
        return
    if value < minimum:
        reasons.append(f"{INVALID_RANGE_PREFIX}{field}")
        return


def _check_int_eq_zero(
    state: Mapping[str, Any],
    reasons: list[str],
    field: str,
) -> None:
    """Fail if *field* is not int or is != 0.  Zero is pristine."""
    value = state.get(field)
    if type(value) is not int:
        reasons.append(f"{INVALID_TYPE_PREFIX}{field}")
        return
    if value != 0:
        code = _FIELD_REASON.get(field, field)
        if value < 0:
            reasons.append(f"{INVALID_RANGE_PREFIX}{field}")
        else:
            # Positive value = durable progress
            reasons.append(code)
        return


def _check_bool_false(
    state: Mapping[str, Any],
    reasons: list[str],
    field: str,
) -> None:
    """Fail if *field* is not bool or is not False."""
    value = state.get(field)
    if type(value) is not bool:
        reasons.append(f"{INVALID_TYPE_PREFIX}{field}")
        return
    if value is not False:
        code = _FIELD_REASON.get(field, field)
        reasons.append(code)
        return


def _check_cursor(
    state: Mapping[str, Any],
    reasons: list[str],
) -> None:
    """Cursor must be a string equal to INITIAL_CURSOR."""
    value = state.get("cursor")
    if type(value) is not str:
        reasons.append(f"{INVALID_TYPE_PREFIX}cursor")
        return
    if value != INITIAL_CURSOR:
        reasons.append(REASON_CURSOR_ADVANCED)
        return


def _check_request_signature(
    state: Mapping[str, Any],
    reasons: list[str],
) -> None:
    """request_signature must be an empty string for unbound state.

    Note: ``is_strictly_pristine_unbound_backfill`` short-circuits on a
    non-empty string, so for that path a non-empty signature never reaches
    this helper.  This only fires for invalid *types* (non-string values)
    or when called directly from ``describe_nonpristine_unbound_backfill``.
    """
    value = state.get("request_signature")
    if type(value) is not str:
        reasons.append(f"{INVALID_TYPE_PREFIX}request_signature")
        return
    if value != "":
        reasons.append(REASON_REQUEST_SIGNATURE_BOUND)
        return


def _check_nullable_text(
    state: Mapping[str, Any],
    reasons: list[str],
    field: str,
) -> None:
    """Nullable text: must be None or a string.  Non-None non-str is an error."""
    value = state.get(field)
    if value is None:
        return  # pristine
    if type(value) is not str:
        reasons.append(f"{INVALID_TYPE_PREFIX}{field}")
        return
    # Non-empty string → non-pristine
    if value != "":
        code = _FIELD_REASON.get(field, field)
        reasons.append(code)
        return
    # Empty string is pristine for last_committed_page_id.
    # For timestamp fields an empty string is also treated as pristine
    # since the schema allows both None and "" as "not set".


def _check_history(
    state: Mapping[str, Any],
    reasons: list[str],
) -> None:
    """generation_history must be a list and must be empty."""
    value = state.get("generation_history")
    if type(value) is not list:
        reasons.append(f"{INVALID_TYPE_PREFIX}generation_history")
        return
    if len(value) > 0:
        reasons.append(REASON_GENERATION_HISTORY)
        return


# ── Backfill bind decision (pure) ────────────────────────────────────
# This function is a pure computation: given a backfill state and a
# requested signature hash, it returns a decision without touching files,
# mutation, or I/O.  The Store layer calls it inside the write lock and
# only commits the result to disk.

from enum import Enum, auto
from typing import Any


class BackfillBindDecision(Enum):
    SAME_SIGNATURE = auto()
    FIRST_BIND = auto()
    ROLL_GENERATION = auto()


class BackfillBindError(ValueError):
    """Raised when an unbound non-pristine backfill cannot accept a signature."""


def validate_backfill_state_exact(
    state: Mapping[str, Any],
    *,
    allow_unbound: bool,
) -> None:
    """Validate a backfill state against the exact v3 schema.

    Parameters
    ----------
    state:
        The backfill state dict to validate.
    allow_unbound:
        If ``True``, an empty ``request_signature`` is allowed only when the
        state is strictly pristine (all counters zero, booleans false, cursor
        at ``INITIAL_CURSOR``, empty history).  If ``False``, the signature must
        be a non-empty 16-hex string.

    Raises
    ------
    BackfillBindError
        The state is missing fields, has unknown fields, or has invalid types /
        ranges.  For unbound states, this also raises when the state is not
        strictly pristine.
    """
    if not isinstance(state, Mapping):
        raise BackfillBindError(
            f"backfill state must be a mapping, got {type(state).__name__}"
        )

    state_keys: Set[str] = set(state)

    missing = sorted(BACKFILL_STATE_FIELDS - state_keys)
    if missing:
        raise BackfillBindError(f"backfill missing fields: {' '.join(missing)}")

    extra = sorted(state_keys - BACKFILL_STATE_ACCEPTED_FIELDS)
    if extra:
        raise BackfillBindError(f"backfill has unknown fields: {' '.join(extra)}")

    # Generation: true int >= 1
    gen = state.get("generation")
    if type(gen) is not int or gen < 1:
        raise BackfillBindError(
            f"backfill generation must be int >= 1, got {gen!r}"
        )

    # request_signature: str, empty or 16-hex
    sig = state.get("request_signature")
    if type(sig) is not str:
        raise BackfillBindError(
            f"backfill request_signature must be a string, got {sig!r}"
        )
    if sig:
        if not _HEX16.fullmatch(sig):
            raise BackfillBindError(
                f"backfill request_signature must be 16 lowercase hex, got {sig!r}"
            )
    elif not allow_unbound:
        raise BackfillBindError(
            "backfill request_signature must be non-empty for a bound state"
        )

    # Cursor: str
    cursor = state.get("cursor")
    if type(cursor) is not str:
        raise BackfillBindError(
            f"backfill cursor must be a string, got {cursor!r}"
        )

    # Boolean flags: true bool
    for field in _BOOL_FIELDS:
        value = state[field]
        if type(value) is not bool:
            raise BackfillBindError(
                f"backfill {field} must be a boolean, got {value!r}"
            )

    # Integer counters: true int >= 0
    for field in _INT_FIELDS:
        value = state[field]
        if type(value) is not int or value < 0:
            raise BackfillBindError(
                f"backfill {field} must be a non-negative integer, got {value!r}"
            )

    # generation_history: list, each entry exact
    history = state.get("generation_history")
    if type(history) is not list:
        raise BackfillBindError(
            f"backfill generation_history must be a list, got {history!r}"
        )
    _validate_generation_history_exact(history)

    # Nullable timestamp/text fields: None or str
    for field in _NULLABLE_TEXT_FIELDS | {"last_committed_page_id"}:
        value = state[field]
        if value is not None and type(value) is not str:
            raise BackfillBindError(
                f"backfill {field} must be None or a string, got {value!r}"
            )

    # Unbound state must be strictly pristine.
    if not sig:
        reasons = _nonpristine_reasons(state)
        if reasons:
            raise BackfillBindError(
                f"non-pristine unbound backfill: {' '.join(reasons)}"
            )


def _validate_generation_history_exact(history: list[Any]) -> None:
    """Validate every entry in *history* for exact schema and ordering."""
    previous = -1
    for index, item in enumerate(history):
        if not isinstance(item, Mapping):
            raise BackfillBindError(
                f"generation_history[{index}] must be a mapping, got {type(item).__name__}"
            )
        entry_keys = set(item)
        # Must match the writer in keyword_notebook.ensure_backfill_generation
        # (which emits generation, request_signature, closed_at, reason,
        # cursor, exhausted, pages_succeeded, pages_committed,
        # items_returned_total, last_committed_page_id).
        required_history_keys = {
            "generation", "request_signature", "closed_at", "reason",
            "cursor", "exhausted", "pages_succeeded", "pages_committed",
            "items_returned_total", "last_committed_page_id",
        }
        missing = sorted(required_history_keys - entry_keys)
        if missing:
            raise BackfillBindError(
                f"generation_history[{index}] missing fields: {' '.join(missing)}"
            )
        extra = sorted(entry_keys - required_history_keys)
        if extra:
            raise BackfillBindError(
                f"generation_history[{index}] has unknown fields: {' '.join(extra)}"
            )
        generation = item["generation"]
        if type(generation) is not int or generation < 1:
            raise BackfillBindError(
                f"generation_history[{index}].generation must be int >= 1, got {generation!r}"
            )
        if generation <= previous:
            raise BackfillBindError(
                f"generation_history[{index}].generation must be strictly increasing"
            )
        previous = generation
        signature = item["request_signature"]
        if type(signature) is not str or (signature and not _HEX16.fullmatch(signature)):
            raise BackfillBindError(
                f"generation_history[{index}].request_signature must be empty or 16 hex, "
                f"got {signature!r}"
            )
        for field in ("closed_at", "reason"):
            value = item[field]
            if type(value) is not str or not value.strip():
                raise BackfillBindError(
                    f"generation_history[{index}].{field} must be a non-blank string, "
                    f"got {value!r}"
                )


def resolve_backfill_generation_binding(
    state: Mapping[str, Any],
    requested_signature: str,
) -> BackfillBindDecision:
    """Determine what action ``ensure_backfill_generation`` should take.

    Parameters
    ----------
    state : Mapping[str, Any]
        The current backfill state dict (read-only).
    requested_signature : str
        The 16-hex signature hash being requested.

    Returns
    -------
    BackfillBindDecision
        ``SAME_SIGNATURE`` — already bound to the requested hash.
        ``FIRST_BIND`` — unbound and strictly pristine; safe to bind.
        ``ROLL_GENERATION`` — already bound to a different hash.

    Raises
    ------
    BackfillBindError
        The state is structurally invalid or unbound but not pristine.
    ValueError
        ``requested_signature`` is malformed.
    """
    # 1. Validate requested_signature.
    if not isinstance(requested_signature, str):
        raise ValueError(
            f"requested_signature must be a str, got {type(requested_signature).__name__}"
        )
    if not _HEX16.fullmatch(requested_signature):
        raise ValueError(
            f"requested_signature must be 16 lowercase hex, got {requested_signature!r}"
        )

    # 2. Validate the current state exactly.  Unbound states are allowed only
    #    when strictly pristine; bound states may carry progress.
    validate_backfill_state_exact(state, allow_unbound=True)

    current_sig = state.get("request_signature")
    # 3. Same signature → no change.
    if current_sig == requested_signature:
        return BackfillBindDecision.SAME_SIGNATURE

    # 4. Empty signature + strict pristine → first bind.
    if not current_sig:
        return BackfillBindDecision.FIRST_BIND

    # 5. Non-empty different signature → roll generation.
    return BackfillBindDecision.ROLL_GENERATION
