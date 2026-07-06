from __future__ import annotations

from pathlib import Path
from typing import Any

from src.services.source_records import (
    metadata_source_rel_path,
    write_metadata_source_record as _write_metadata_source_record,
)


def write_metadata_source_record(
    folder: Path,
    provider: str = "manual",
    payload: dict[str, Any] | None = None,
) -> Path:
    """Write a canonical metadata source sidecar and return its path."""
    record = payload or {"provider": provider, "record": {"title": "Example Paper"}}
    return _write_metadata_source_record(folder, provider, record)


def metadata_source_path(provider: str = "manual") -> str:
    return metadata_source_rel_path(provider)
