"""ReportStoreV4 — persist batch discovery reports in a workspace."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from src.discovery.workspace import DiscoveryWorkspace


class ReportStoreV4:
    """Persist batch discovery reports under ``reports/``."""

    def __init__(self, workspace: DiscoveryWorkspace) -> None:
        self._workspace = workspace
        self._dir = workspace.reports_dir

    @property
    def workspace(self) -> DiscoveryWorkspace:
        return self._workspace

    def save(self, report: dict[str, Any], filename: str | None = None) -> Path:
        """Atomically save a batch report. Returns the file path."""
        self._dir.mkdir(parents=True, exist_ok=True)
        name = filename or f"report_{_now_compact()}.json"
        path = self._dir / name

        payload = json.dumps(report, ensure_ascii=False, indent=2)
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

    def list_reports(self) -> list[Path]:
        """List all report files."""
        if not self._dir.is_dir():
            return []
        return sorted(self._dir.glob("*.json"))


def _now_compact() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
