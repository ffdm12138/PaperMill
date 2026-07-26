"""PDF fetch-result sidecar writer.

Lives in the fetch layer (not metadata/source_records) because persisting a
fetch result sanitizes transport data via ``pdf_transport``; the metadata
layer must never depend on fetch.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from src.fetch.pdf_transport import sanitize_for_persistence
from src.metadata.source_records import (
    FETCH_RESULT_FILENAME,
    resolve_safe_source_record_target,
)
from src.utils.atomic_io import atomic_write_json


def write_fetch_result(
    folder: str | Path,
    fetch_record: dict[str, Any],
) -> Path:
    """Write a PDF fetch result record to ``source_records/fetch_result.json``.

    This is the ONLY writer for fetch results. It must never overwrite a
    metadata source record.
    """
    path = resolve_safe_source_record_target(Path(folder), FETCH_RESULT_FILENAME)
    atomic_write_json(path, {"fetch_result": sanitize_for_persistence(fetch_record)}, indent=2)
    resolve_safe_source_record_target(Path(folder), path.name)
    return path
