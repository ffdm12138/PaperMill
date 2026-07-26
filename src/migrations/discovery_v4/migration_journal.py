"""Discovery v4 migration journal — durable state tracking.

States: planned → inventory_complete → archive_prepared → workspace_built
     → notebooks_staged → candidates_extracted → preflight_validated
     → smoke_failed (recoverable) → smoke_passed → cutover_committed
     → legacy_cleaned → finalized

ABORTED is a terminal state reachable from any pre-cutover state, and from
cutover_committed via --rollback (the only post-cutover escape).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from filelock import FileLock

from config.settings import DISCOVERY_MIGRATIONS_DIR

MIGRATION_JOURNAL_SCHEMA = "1.0"


class MigrationState(str, Enum):
    PLANNED = "planned"
    INVENTORY_COMPLETE = "inventory_complete"
    ARCHIVE_PREPARED = "archive_prepared"
    NOTEBOOKS_STAGED = "notebooks_staged"
    CANDIDATES_EXTRACTED = "candidates_extracted"
    WORKSPACE_BUILT = "workspace_built"
    PREFLIGHT_VALIDATED = "preflight_validated"
    SMOKE_FAILED = "smoke_failed"
    SMOKE_PASSED = "smoke_passed"
    CUTOVER_COMMITTED = "cutover_committed"
    LEGACY_CLEANED = "legacy_cleaned"
    FINALIZED = "finalized"
    ABORTED = "aborted"


# Ordered happy-path states; adjacent transitions only.
_ORDERED_STATES = (
    MigrationState.PLANNED,
    MigrationState.INVENTORY_COMPLETE,
    MigrationState.ARCHIVE_PREPARED,
    MigrationState.WORKSPACE_BUILT,
    MigrationState.NOTEBOOKS_STAGED,
    MigrationState.CANDIDATES_EXTRACTED,
    MigrationState.PREFLIGHT_VALIDATED,
    MigrationState.SMOKE_PASSED,
    MigrationState.CUTOVER_COMMITTED,
    MigrationState.LEGACY_CLEANED,
    MigrationState.FINALIZED,
)

VALID_TRANSITIONS: dict[MigrationState, frozenset[MigrationState]] = {}
for _i, _s in enumerate(_ORDERED_STATES):
    _next_states: set[MigrationState] = set()
    if _i + 1 < len(_ORDERED_STATES):
        _next_states.add(_ORDERED_STATES[_i + 1])
    VALID_TRANSITIONS[_s] = frozenset(_next_states)

# Smoke failure is a recoverable side-branch from preflight.
VALID_TRANSITIONS[MigrationState.PREFLIGHT_VALIDATED] = frozenset(
    VALID_TRANSITIONS[MigrationState.PREFLIGHT_VALIDATED] | {MigrationState.SMOKE_FAILED}
)
VALID_TRANSITIONS[MigrationState.SMOKE_FAILED] = frozenset(
    {MigrationState.SMOKE_PASSED, MigrationState.ABORTED}
)

# ABORTED is terminal and may be entered from any pre-cutover state, and from
# CUTOVER_COMMITTED via --rollback.  LEGACY_CLEANED and FINALIZED never abort.
for _s in MigrationState:
    if _s not in {
        MigrationState.LEGACY_CLEANED,
        MigrationState.FINALIZED,
        MigrationState.ABORTED,
    }:
        VALID_TRANSITIONS[_s] = frozenset(VALID_TRANSITIONS[_s] | {MigrationState.ABORTED})

VALID_TRANSITIONS[MigrationState.ABORTED] = frozenset()  # terminal


@dataclass
class MigrationJournal:
    """Durable migration progress tracker with atomic state transitions."""

    migration_id: str
    state: MigrationState = MigrationState.PLANNED
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    transitions: list[dict[str, str]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def transition_to(self, new_state: MigrationState) -> None:
        allowed = VALID_TRANSITIONS.get(self.state, frozenset())
        if new_state not in allowed:
            raise ValueError(
                f"cannot transition from {self.state.value} to {new_state.value}. "
                f"Allowed: {sorted(s.value for s in allowed)}"
            )
        now = datetime.now(timezone.utc).isoformat()
        self.transitions.append({
            "from": self.state.value,
            "to": new_state.value,
            "at": now,
        })
        self.state = new_state

    @property
    def path(self) -> Path:
        return DISCOVERY_MIGRATIONS_DIR / f"{self.migration_id}.json"

    def save(self) -> None:
        """Atomically persist the migration journal."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(self.path.with_suffix(self.path.suffix + ".lock")))
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps({
            "schema_version": MIGRATION_JOURNAL_SCHEMA,
            "migration_id": self.migration_id,
            "state": self.state.value,
            "created_at": self.created_at,
            "transitions": self.transitions,
            "metadata": self.metadata,
        }, ensure_ascii=False, indent=2)
        raw = payload.encode("utf-8")
        try:
            with lock:
                with tmp.open("wb") as fh:
                    fh.write(raw)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(str(tmp), str(self.path))
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    @classmethod
    def load(cls, migration_id: str) -> "MigrationJournal":
        path = DISCOVERY_MIGRATIONS_DIR / f"{migration_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"migration journal not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != MIGRATION_JOURNAL_SCHEMA:
            raise ValueError(
                f"unknown migration journal schema: {data.get('schema_version')}"
            )
        return cls(
            migration_id=data["migration_id"],
            state=MigrationState(data["state"]),
            created_at=data.get("created_at", ""),
            transitions=data.get("transitions", []),
            metadata=data.get("metadata", {}),
        )

    @classmethod
    def create(cls, migration_id: str, **metadata: Any) -> "MigrationJournal":
        """Create a new migration journal. Fails if one already exists."""
        journal = cls(migration_id=migration_id, metadata=dict(metadata))
        if journal.path.exists():
            raise FileExistsError(f"migration journal already exists: {journal.path}")
        journal.save()
        return journal
