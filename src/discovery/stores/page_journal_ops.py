"""Mutation/IO-side page journal helpers owned by the stores layer.

``src.discovery.contracts.page_journal`` is the strict data + validation
contract: schema fields, state-machine vocabulary, identity hashes, and
pure validation/transformation.  The helpers here apply candidate
mutations, enforce write-path protocol, or touch the filesystem, so they
live next to the durable journal writers (``PageJournalStoreV4``).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from src.discovery.contracts.page_journal import (
    CANDIDATE_TRANSITIONS,
    CandidateState,
    InvalidStateTransition,
    TERMINAL_CANDIDATE_STATES,
)
from src.discovery.relevance import RELEVANCE_STATES, RelevanceState


def path_is_reparse(path: Path) -> bool:
    """Detect symlinks and Windows reparse points without following them."""
    try:
        info = path.lstat()
    except OSError:
        return True  # cannot stat → treat as unsafe
    if hasattr(os.path, 'islink') and os.path.islink(path):  # noqa: PTH111
        return True
    import stat as _stat_mod
    attrs = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(_stat_mod, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attrs & reparse_flag)


def write_page_json_unlocked(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically (caller holds lock) with fsync durability.

    Delegates to :func:`src.utils.atomic_io.atomic_write_json_unlocked`
    so that all durable writers share the same fsync + tmp + os.replace
    + parent-dir-fsync implementation.
    """
    from src.utils.atomic_io import atomic_write_json_unlocked as _unlocked

    _unlocked(path, data, indent=2)


def transition_candidate(item: dict[str, Any], new_state: CandidateState) -> None:
    old = item.get("status")
    if new_state not in CANDIDATE_TRANSITIONS.get(old, set()):
        if old == new_state:
            return
        raise InvalidStateTransition(f"candidate {old} -> {new_state} is not allowed")
    item["status"] = new_state


def assert_terminal_replay_equivalent(
    item: Mapping[str, Any],
    *,
    new_status: str,
    updates: Mapping[str, Any] | None,
) -> bool:
    """Allow terminal replay only when it is a byte-preserving no-op."""
    old_status = str(item.get("status") or "")
    if old_status not in TERMINAL_CANDIDATE_STATES:
        return False
    if new_status != old_status:
        raise InvalidStateTransition(
            f"terminal candidate replay cannot change status {old_status} -> {new_status}"
        )
    mismatched = sorted(
        key
        for key, value in (updates or {}).items()
        if key not in item or item[key] != value
    )
    if mismatched:
        raise InvalidStateTransition(
            "terminal candidate replay cannot overwrite fields: "
            + ",".join(mismatched)
        )
    return True


def assert_relevance_finalized(data: dict[str, Any], path: Path) -> None:
    """Every candidate must carry an explicit, non-profile_unbound relevance record.

    Called before ``mark_cursor_committed`` transitions the page state.
    This covers both the normal path and the all-terminal fast-path to
    ``drained``.  ``profile_unbound`` or a missing relevance record are
    always rejected: a new page must be evaluated before its cursor can
    advance.
    """
    allowed = RELEVANCE_STATES - {RelevanceState.PROFILE_UNBOUND}
    for item in data.get("candidates", []):
        relevance = item.get("relevance")
        if not isinstance(relevance, Mapping):
            raise InvalidStateTransition(
                f"candidate {item.get('candidate_id')!r} is missing a "
                f"relevance record and cannot be cursor-committed: {path}"
            )
        state = str(relevance.get("state") or "")
        if state == RelevanceState.PROFILE_UNBOUND:
            raise InvalidStateTransition(
                f"candidate {item.get('candidate_id')!r} is still "
                f"profile_unbound and cannot be cursor-committed: {path}"
            )
        if state not in allowed:
            raise InvalidStateTransition(
                f"candidate {item.get('candidate_id')!r} has unknown "
                f"relevance state {state!r}: {path}"
            )
