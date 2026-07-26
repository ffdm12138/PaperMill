"""LaneStateStoreV4 — CAS cursor transactions for v4 lane state.

Cursor advancement requires a durable v4 page journal plus expected
revision and expected cursor.  There is no evidence-free cursor API.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from filelock import FileLock

from src.discovery.contracts.lane_state import (
    LANE_STATE_SCHEMA_VERSION_V4,
    CursorTransactionV4,
    LaneStateV4,
)
from src.discovery.workspace import DiscoveryWorkspace
from src.utils.atomic_io import atomic_replace_bytes_unlocked


class LaneStateStoreV4:
    """CAS-based lane state persistence.

    Every cursor advancement is a Compare-And-Swap transaction:
    ``commit_provider_page(expected_revision, expected_cursor, durable_page)``.
    """

    def __init__(self, workspace: DiscoveryWorkspace) -> None:
        self._workspace = workspace
        self._dir = workspace.lane_states_dir

    @property
    def workspace(self) -> DiscoveryWorkspace:
        return self._workspace

    def _lane_path(self, keyword_id: str, query_id: str,
                   provider: str, mode: str) -> Path:
        """Canonical path for a lane state file."""
        return self._dir / f"{keyword_id}_{query_id}_{provider}_{mode}.json"

    def read(self, keyword_id: str, query_id: str,
             provider: str, mode: str) -> LaneStateV4 | None:
        """Read a lane state. Returns None if missing or corrupt."""
        path = self._lane_path(keyword_id, query_id, provider, mode)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            return LaneStateV4.from_dict_strict(data)
        except (ValueError, TypeError):
            return None

    def read_or_create(self, keyword_id: str, query_id: str,
                       provider: str, mode: str,
                       query_language: str = "zh") -> LaneStateV4:
        """Read existing lane state or create a pristine one."""
        existing = self.read(keyword_id, query_id, provider, mode)
        if existing is not None:
            return existing
        return LaneStateV4(
            keyword_id=keyword_id,
            query_id=query_id,
            provider=provider,
            mode=mode,
            query_language=query_language,
        )

    def write(self, state: LaneStateV4) -> Path:
        """Persist a lane state atomically. Returns the file path."""
        path = self._lane_path(
            state.keyword_id, state.query_id, state.provider, state.mode
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(path.with_suffix(path.suffix + ".lock")))

        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
        raw = payload.encode("utf-8")

        with lock:
            atomic_replace_bytes_unlocked(path, raw)

        return path

    def commit_provider_page(
        self,
        keyword_id: str,
        query_id: str,
        provider: str,
        mode: str,
        expected_revision: int,
        expected_cursor: str,
        new_cursor: str,
        page_path: str,
        page_id: str,
    ) -> LaneStateV4:
        """CAS cursor advancement.

        Transaction order:
        1. Read current lane state
        2. Verify revision matches expected_revision
        3. Verify cursor matches expected_cursor
        4. Write updated lane state with new cursor and incremented revision
        5. On failure, leave no partial state

        Raises ValueError on CAS mismatch.
        """
        current = self.read_or_create(keyword_id, query_id, provider, mode)

        if current.revision != expected_revision:
            raise ValueError(
                f"CAS revision mismatch: expected {expected_revision}, "
                f"actual {current.revision} for lane {current.lane_key_str}"
            )
        if current.cursor != expected_cursor:
            raise ValueError(
                f"CAS cursor mismatch: expected {expected_cursor!r}, "
                f"actual {current.cursor!r} for lane {current.lane_key_str}"
            )

        transaction = CursorTransactionV4(
            keyword_id=keyword_id,
            query_id=query_id,
            provider=provider,
            mode=mode,
            expected_revision=expected_revision,
            expected_cursor=expected_cursor,
            new_cursor=new_cursor,
            new_revision=expected_revision + 1,
            page_path=page_path,
        )

        updated = LaneStateV4(
            keyword_id=keyword_id,
            query_id=query_id,
            provider=provider,
            mode=mode,
            query_language=current.query_language,
            cursor=new_cursor,
            exhausted=current.exhausted,
            generation=current.generation,
            last_committed_page_id=page_id,
            exhaustion_evidence_id=current.exhaustion_evidence_id,
            revision=transaction.new_revision,
        )

        self.write(updated)
        return updated

    def list_all(self) -> list[Path]:
        """List all lane state files."""
        if not self._dir.is_dir():
            return []
        return sorted(self._dir.glob("*.json"))
