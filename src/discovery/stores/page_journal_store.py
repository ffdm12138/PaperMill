"""PageJournalStoreV4 — read/write/list v4 page journals from a workspace.

All writes use atomic tmp+fsync+os.replace.  Only schema 4.0 journals
are accepted.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from filelock import FileLock

from src.discovery.contracts.page_journal import (
    PAGE_SCHEMA_VERSION_V4,
    PAGE_V4_FIELDS,
    ProviderPageJournalV4,
    UnexpectedNonV4StateError,
)
from src.discovery.workspace import DiscoveryWorkspace


class PageJournalStoreV4:
    """Read and write v4 provider page journals from a DiscoveryWorkspace.

    Path layout: ``page_journals/<keyword_id>/<query_id>/<provider>/<lane>/<page_id>.json``
    """

    def __init__(self, workspace: DiscoveryWorkspace) -> None:
        self._workspace = workspace
        self._dir = workspace.page_journals_dir

    @property
    def workspace(self) -> DiscoveryWorkspace:
        return self._workspace

    @property
    def root_dir(self) -> Path:
        return self._dir

    def _page_path(
        self,
        keyword_id: str,
        query_id: str,
        provider: str,
        lane: str,
        page_id: str,
    ) -> Path:
        return (
            self._dir / keyword_id / query_id / provider / lane / f"{page_id}.json"
        )

    def write(self, journal: ProviderPageJournalV4) -> Path:
        """Atomically persist a v4 page journal. Returns the file path."""
        d = journal.to_dict()
        path = self._page_path(
            d["keyword_id"], d["query_id"], d["provider"], d["lane"], d["page_id"]
        )
        path.parent.mkdir(parents=True, exist_ok=True)

        payload = json.dumps(d, ensure_ascii=False, indent=2)
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

    def read(self, path: Path | str) -> ProviderPageJournalV4 | None:
        """Read a v4 page journal. Returns None if missing or non-v4."""
        p = Path(path)
        if not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        sv = data.get("schema_version")
        if sv != PAGE_SCHEMA_VERSION_V4:
            return None
        try:
            return ProviderPageJournalV4.from_dict_strict(data)
        except (ValueError, TypeError):
            return None

    def read_strict(self, path: Path | str) -> ProviderPageJournalV4:
        """Read a v4 page journal. Raises on non-v4 or corrupt files."""
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"page journal not found: {p}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            raise UnexpectedNonV4StateError(
                f"cannot read page journal {p}: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise UnexpectedNonV4StateError(
                f"page journal {p} is not a JSON object"
            )
        sv = data.get("schema_version")
        if sv != PAGE_SCHEMA_VERSION_V4:
            raise UnexpectedNonV4StateError(
                f"page journal {p} has schema_version {sv!r}, "
                f"expected {PAGE_SCHEMA_VERSION_V4!r}"
            )
        return ProviderPageJournalV4.from_dict_strict(data)

    def list_all(self) -> list[Path]:
        """List all page journal files recursively."""
        if not self._dir.is_dir():
            return []
        return sorted(self._dir.rglob("*.json"))

    def list_by_keyword(self, keyword_id: str) -> list[Path]:
        """List page journals for one keyword."""
        kd = self._dir / keyword_id
        if not kd.is_dir():
            return []
        return sorted(kd.rglob("*.json"))
