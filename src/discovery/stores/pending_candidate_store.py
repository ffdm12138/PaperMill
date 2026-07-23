"""PendingCandidateStoreV4 — read/write/list pending candidates in a workspace.

Pending candidates are written atomically and indexed for backpressure
tracking.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from src.discovery.contracts.candidate import PendingCandidateV4
from src.discovery.workspace import DiscoveryWorkspace


class PendingCandidateStoreV4:
    """Persist pending candidates under ``pending_candidates/``.

    Path layout: ``pending_candidates/<keyword_id>/<candidate_id>.json``

    Backpressure tracking uses the count of pending files as its
    primary signal — never ``queue.Full``.
    """

    def __init__(self, workspace: DiscoveryWorkspace) -> None:
        self._workspace = workspace
        self._dir = workspace.pending_candidates_dir

    @property
    def workspace(self) -> DiscoveryWorkspace:
        return self._workspace

    @property
    def root_dir(self) -> Path:
        return self._dir

    def write(self, candidate: PendingCandidateV4) -> Path:
        """Atomically persist a pending candidate. Returns the file path."""
        kid = candidate.keyword_id or "unknown"
        cid = candidate.candidate_id or "unknown"
        path = self._dir / kid / f"{cid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)

        payload = json.dumps(candidate.to_dict(), ensure_ascii=False, indent=2)
        raw = payload.encode("utf-8")
        tmp = path.with_suffix(path.suffix + ".tmp")

        try:
            with tmp.open("wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(str(tmp), str(path))
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

        return path

    def read(self, keyword_id: str, candidate_id: str) -> PendingCandidateV4 | None:
        """Read a pending candidate. Returns None if missing."""
        path = self._dir / keyword_id / f"{candidate_id}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            return PendingCandidateV4.from_dict_strict(data)
        except (ValueError, TypeError):
            return None

    def count(self) -> int:
        """Total number of pending candidate files."""
        if not self._dir.is_dir():
            return 0
        return len(list(self._dir.rglob("*.json")))

    def count_by_keyword(self, keyword_id: str) -> int:
        """Count pending candidates for one keyword."""
        kd = self._dir / keyword_id
        if not kd.is_dir():
            return 0
        return len(list(kd.rglob("*.json")))

    def list_all(self) -> list[Path]:
        """List all pending candidate files."""
        if not self._dir.is_dir():
            return []
        return sorted(self._dir.rglob("*.json"))

    def list_by_keyword(self, keyword_id: str) -> list[Path]:
        """List pending candidates for one keyword."""
        kd = self._dir / keyword_id
        if not kd.is_dir():
            return []
        return sorted(kd.rglob("*.json"))

    def delete(self, keyword_id: str, candidate_id: str) -> bool:
        """Delete a processed candidate. Returns True if deleted."""
        path = self._dir / keyword_id / f"{candidate_id}.json"
        try:
            path.unlink()
            return True
        except OSError:
            return False
