"""Centralized ledger lifecycle state machine.

Defines the canonical state set, the sole allowed-transition table, and a typed
transition function. Every ledger state write — whether from the allocator,
commit, rollback, repair CLI, or recovery — MUST pass through
:func:`assert_ledger_transition`. Direct ``item["state"] = ...`` writes are
forbidden.

Ledger states are strictly lifecycle positions. Operation results (staging
failure, conversion failure, etc.) belong in ``.import_status.json`` and the
candidate journal — never in the ledger.
"""
from __future__ import annotations

from typing import Any


LEDGER_ALLOCATING = "allocating"
LEDGER_RESERVED = "reserved"
LEDGER_METADATA_STAGED = "metadata_staged"
LEDGER_ACTIVE = "active"
LEDGER_ABANDONED = "abandoned"

ALL_LEDGER_STATES = frozenset({
    LEDGER_ALLOCATING,
    LEDGER_RESERVED,
    LEDGER_METADATA_STAGED,
    LEDGER_ACTIVE,
    LEDGER_ABANDONED,
})

# Terminal states — workspaces that are permanently removed from active
# circulation. Registry scans, refresh, and DOI matching skip these.
TERMINAL_LEDGER_STATES = frozenset({
    LEDGER_ABANDONED,
})

# Canonical transition table.  A transition NOT listed here is ILLEGAL and
# MUST raise :class:`InvalidLedgerTransition`.
#
# Semantics:
#   allocating → reserved           normal reserve after mkdir + marker write
#   allocating → abandoned          crash between number claim and marker write
#   reserved → metadata_staged      metadata + source record + receipt persisted
#   reserved → abandoned            explicit quarantine / irrecoverable staging
#   metadata_staged → active        formalize→commit completes
#   metadata_staged → abandoned     explicit abandon of a fully-staged workspace
#   active → metadata_staged        explicit formal-library rollback (NOT commit)
#   abandoned → (none)              once abandoned, never auto-recycled
ALLOWED_LEDGER_TRANSITIONS: dict[str, frozenset[str]] = {
    LEDGER_ALLOCATING: frozenset({LEDGER_RESERVED, LEDGER_ABANDONED}),
    LEDGER_RESERVED: frozenset({LEDGER_METADATA_STAGED, LEDGER_ABANDONED}),
    LEDGER_METADATA_STAGED: frozenset({LEDGER_ACTIVE, LEDGER_ABANDONED}),
    LEDGER_ACTIVE: frozenset({LEDGER_METADATA_STAGED}),
    LEDGER_ABANDONED: frozenset(),
}

# Explicit operator repair transitions are kept separate from the normal
# lifecycle table so ordinary ingest code cannot accidentally move a
# confirmed staging checkpoint backwards.
ALLOWED_REPAIR_TRANSITIONS: dict[str, frozenset[str]] = {
    LEDGER_METADATA_STAGED: frozenset({LEDGER_RESERVED}),
}


class InvalidLedgerTransition(ValueError):
    """Raised when a state transition is not in the allowed table."""

    def __init__(
        self,
        number: str,
        current_state: str,
        target_state: str,
        *,
        reason: str = "",
    ) -> None:
        self.number = number
        self.current_state = current_state
        self.target_state = target_state
        msg = (
            f"Invalid ledger transition for {number}: "
            f"{current_state} → {target_state}"
        )
        if reason:
            msg += f" ({reason})"
        super().__init__(msg)


def assert_ledger_transition(
    *,
    paper_number: str,
    current_state: str,
    target_state: str,
    reason: str = "",
) -> None:
    """Validate a ledger state transition against the canonical table.

    This is a validation-only gate. It does NOT touch the ledger dict or disk;
    callers must apply the new state to the ledger item and persist.

    Args:
        paper_number: 16-digit paper_number.
        current_state: The REAL current state read from the ledger item.
        target_state: The destination state.
        reason: Optional diagnostic note (for error messages only).

    Raises:
        InvalidLedgerTransition: if the transition is not in
            :data:`ALLOWED_LEDGER_TRANSITIONS`.
        ValueError: if *current_state* or *target_state* is not a known state.
    """
    if current_state not in ALL_LEDGER_STATES:
        raise InvalidLedgerTransition(
            paper_number, current_state, target_state,
            reason=f"unknown current state: {current_state!r}",
        )
    if target_state not in ALL_LEDGER_STATES:
        raise InvalidLedgerTransition(
            paper_number, current_state, target_state,
            reason=f"unknown target state: {target_state!r}",
        )
    allowed = ALLOWED_LEDGER_TRANSITIONS.get(current_state, frozenset())
    if target_state not in allowed:
        raise InvalidLedgerTransition(
            paper_number, current_state, target_state,
            reason=reason or f"not in allowed transitions from {current_state}",
        )


def assert_ledger_repair_transition(
    *, paper_number: str, current_state: str, target_state: str,
    reason: str = "",
) -> None:
    """Validate a transition exposed only by an explicit repair command."""
    if current_state not in ALL_LEDGER_STATES or target_state not in ALL_LEDGER_STATES:
        raise InvalidLedgerTransition(
            paper_number, current_state, target_state,
            reason=reason or "unknown repair lifecycle state",
        )
    if target_state not in ALLOWED_REPAIR_TRANSITIONS.get(current_state, frozenset()):
        raise InvalidLedgerTransition(
            paper_number, current_state, target_state,
            reason=reason or "not an allowed repair transition",
        )


def build_transition_patch(
    existing_item: dict[str, Any] | None,
    *,
    number: str,
    target_state: str,
    folder: str,
    folder_path: str,
    planned_paper_name: str = "",
    paper_name: str = "",
    extra: dict[str, Any] | None = None,
    now_iso: str = "",
) -> dict[str, Any]:
    """Build a ledger item dict for a state transition, preserving known fields.

    Callers should use this to construct the new ``items[number]`` value, then
    persist the ledger dict atomically. This ensures every transition carries
    consistent timestamps and identity fields without duplicating patch logic
    across sites.
    """
    import datetime

    if not now_iso:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    existing = existing_item or {}
    base: dict[str, Any] = {
        "folder_name": Path(folder).name if folder else existing.get("folder_name", ""),
        "folder_path": folder_path,
        "planned_paper_name": planned_paper_name or existing.get("planned_paper_name", ""),
        "state": target_state,
        "created_at": existing.get("created_at", now_iso),
    }
    if paper_name or existing.get("paper_name"):
        base["paper_name"] = paper_name or existing.get("paper_name", "")

    # Timestamp conventions per target state.
    ts_map = {
        LEDGER_ALLOCATING: "created_at",
        LEDGER_RESERVED: "reserved_at",
        LEDGER_METADATA_STAGED: "metadata_staged_at",
        LEDGER_ACTIVE: "activated_at",
        LEDGER_ABANDONED: "abandoned_at",
    }
    ts_key = ts_map.get(target_state)
    if ts_key:
        base[ts_key] = now_iso

    if extra:
        base.update(extra)

    return base


# Re-export for convenience so callers can import from one place.
from pathlib import Path  # noqa: E402
