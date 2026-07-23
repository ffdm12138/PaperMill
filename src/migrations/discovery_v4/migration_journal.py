"""Discovery v4 migration journal — durable state tracking.

States: planned → inventory_complete → archive_prepared → notebooks_staged
     → candidates_extracted → workspace_built → preflight_validated
     → smoke_passed → cutover_committed → legacy_cleaned → finalized
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
    SMOKE_PASSED = "smoke_passed"
    CUTOVER_COMMITTED = "cutover_committed"
    LEGACY_CLEANED = "legacy_cleaned"
    FINALIZED = "finalized"


# Ordered states — any state can transition to any later state
_ORDERED_STATES = tuple(MigrationState)
VALID_TRANSITIONS: dict[MigrationState, frozenset[MigrationState]] = {}
for _i, _s in enumerate(_ORDERED_STATES):
    VALID_TRANSITIONS[_s] = frozenset(_ORDERED_STATES[_i + 1:])


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
