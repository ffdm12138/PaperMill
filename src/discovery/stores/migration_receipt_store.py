"""MigrationReceiptStoreV4 — idempotent seed import receipts."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from src.discovery.workspace import DiscoveryWorkspace


ReceiptStatus = Literal[
    "imported", "already_existing", "duplicate_seed",
    "invalid_doi", "unresolved", "failed",
]


class MigrationReceiptStoreV4:
    """Tracks which legacy candidate seeds have been imported.

    Stores one receipt per seed_id under ``migration_receipts/``
    to ensure idempotent re-import.
    """

    def __init__(self, workspace: DiscoveryWorkspace) -> None:
        self._workspace = workspace
        self._dir = workspace.root / "migration_receipts"

    @property
    def workspace(self) -> DiscoveryWorkspace:
        return self._workspace

    def write_receipt(
        self, seed_id: str, status: ReceiptStatus, metadata: dict[str, Any] | None = None
    ) -> Path:
        """Record a seed import receipt. Idempotent — overwrites same seed_id."""
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{seed_id}.json"
        payload_data: dict[str, Any] = {
            "seed_id": seed_id,
            "status": status,
            "metadata": metadata or {},
        }
        payload = json.dumps(payload_data, ensure_ascii=False, indent=2)
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

    def has_receipt(self, seed_id: str) -> bool:
        """Check if a seed has already been processed."""
        return (self._dir / f"{seed_id}.json").is_file()

    def get_status(self, seed_id: str) -> ReceiptStatus | None:
        """Get the status of a seed import, or None if not processed."""
        path = self._dir / f"{seed_id}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None
        return data.get("status")

    def count_by_status(self) -> dict[str, int]:
        """Count receipts by status."""
        counts: dict[str, int] = {}
        if not self._dir.is_dir():
            return counts
        for path in self._dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                status = data.get("status", "unknown")
                counts[status] = counts.get(status, 0) + 1
            except (json.JSONDecodeError, OSError):
                pass
        return counts
