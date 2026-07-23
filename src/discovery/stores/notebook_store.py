"""NotebookStoreV4 — read/write/list v4 notebooks from a workspace.

All writes use atomic tmp+fsync+os.replace.  Only schema 4.0 notebooks
are accepted — v3 acceptance is NOT provided by this store.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from filelock import FileLock

from src.discovery.workspace import DiscoveryWorkspace


class NotebookStoreV4:
    """Read and write v4 keyword notebooks from a DiscoveryWorkspace.

    The store is workspace-scoped — it resolves all paths from the
    injected ``DiscoveryWorkspace`` and never reads ``config.settings``
    discovery constants.
    """

    def __init__(self, workspace: DiscoveryWorkspace) -> None:
        self._workspace = workspace
        self._dir = workspace.keyword_notebook_dir

    @property
    def workspace(self) -> DiscoveryWorkspace:
        return self._workspace

    @property
    def notebook_dir(self) -> Path:
        return self._dir

    def list_notebooks(self) -> list[Path]:
        """Return sorted list of notebook JSON files."""
        self._dir.mkdir(parents=True, exist_ok=True)
        return sorted(self._dir.glob("*.json"))

    def load(self, path: Path | str) -> dict[str, Any] | None:
        """Load a single notebook dict, or None if missing/corrupt."""
        p = Path(path)
        if not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("schema_version") != "4.0":
            return None  # v4-only — other versions are invisible
        return data

    def load_all(self) -> dict[str, dict[str, Any]]:
        """Load all v4 notebooks keyed by keyword_id."""
        result: dict[str, dict[str, Any]] = {}
        for path in self.list_notebooks():
            nb = self.load(path)
            if nb is not None and "keyword_id" in nb:
                result[nb["keyword_id"]] = nb
        return result

    def save(self, notebook: dict[str, Any]) -> Path:
        """Atomically save a v4 notebook dict.

        The notebook must have ``keyword_id`` and ``keyword_zh`` set.
        Returns the written path.
        """
        kid = notebook.get("keyword_id", "")
        kw_zh = notebook.get("keyword_zh", "unknown")
        if not kid:
            raise ValueError("notebook missing keyword_id")
        if notebook.get("schema_version") != "4.0":
            raise ValueError(
                f"NotebookStoreV4 only writes schema 4.0, "
                f"got {notebook.get('schema_version')!r}"
            )

        self._dir.mkdir(parents=True, exist_ok=True)
        fp8 = kid[:8]
        out_path = self._dir / f"{kw_zh}__{fp8}.json"
        lock = FileLock(str(out_path.with_suffix(out_path.suffix + ".lock")))

        payload = json.dumps(notebook, ensure_ascii=False, indent=2)
        raw = payload.encode("utf-8")
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")

        try:
            with lock:
                with tmp.open("wb") as fh:
                    fh.write(raw)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(str(tmp), str(out_path))
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

        return out_path
