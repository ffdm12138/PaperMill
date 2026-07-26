"""PendingCandidateStoreV4 — read/write/list pending candidates in a workspace.

Pending candidates are written atomically with create-if-absent semantics:
two different candidates that collide on ``(keyword_id, candidate_id)``
raise :class:`CandidateIdentityCollisionError` instead of silently
overwriting each other; an identical rewrite is an idempotent success.
Corrupt files are never mistaken for absent ones — :func:`read` raises
:class:`PendingCandidateCorruptError`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from src.discovery.contracts.candidate import PendingCandidateV4
from src.discovery.workspace import DiscoveryWorkspace


class CandidateIdentityCollisionError(RuntimeError):
    """A different candidate already occupies this (keyword_id, candidate_id)."""


class PendingCandidateCorruptError(RuntimeError):
    """A pending candidate file exists but is unreadable or invalid."""


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
        """Persist a pending candidate; never silently overwrite.

        * absent → create atomically (tmp file + hard-link rename);
        * present with identical payload → idempotent success;
        * present with a different payload →
          :class:`CandidateIdentityCollisionError`.
        """
        kid = candidate.keyword_id or "unknown"
        cid = candidate.candidate_id or "unknown"
        path = self._dir / kid / f"{cid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)

        payload = json.dumps(candidate.to_dict(), ensure_ascii=False, indent=2)
        raw = payload.encode("utf-8")

        if path.exists():
            if path.read_bytes() == raw:
                return path
            raise CandidateIdentityCollisionError(
                f"pending candidate identity collision at {path}: a different "
                f"candidate already owns ({kid}, {cid})"
            )

        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp.open("xb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            # Hard-link rename: atomic and fails if the target appeared
            # between the existence check and now.
            os.link(str(tmp), str(path))
            tmp.unlink()
        except FileExistsError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            if path.read_bytes() == raw:
                return path
            raise CandidateIdentityCollisionError(
                f"pending candidate identity collision at {path}: a different "
                f"candidate already owns ({kid}, {cid})"
            ) from None
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        return path

    def read(self, keyword_id: str, candidate_id: str) -> PendingCandidateV4 | None:
        """Read a pending candidate.

        Returns ``None`` only when the file is absent.  A corrupt or
        schema-violating file raises :class:`PendingCandidateCorruptError`
        — corruption is never mistaken for absence.
        """
        path = self._dir / keyword_id / f"{candidate_id}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            raise PendingCandidateCorruptError(
                f"pending candidate file is corrupt: {path}: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise PendingCandidateCorruptError(
                f"pending candidate file is not a JSON object: {path}"
            )
        try:
            return PendingCandidateV4.from_dict_strict(data)
        except (ValueError, TypeError) as exc:
            raise PendingCandidateCorruptError(
                f"pending candidate violates the v4 contract: {path}: {exc}"
            ) from exc

    def count(self) -> int:
        """Total number of pending candidate files.

        Only files at the canonical ``<keyword_id>/<candidate_id>.json``
        depth are counted; stray JSON files in the root (or deeper) never
        inflate the backpressure signal.
        """
        if not self._dir.is_dir():
            return 0
        total = 0
        for child in self._dir.iterdir():
            if child.is_dir():
                total += sum(1 for f in child.glob("*.json") if f.is_file())
        return total

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
