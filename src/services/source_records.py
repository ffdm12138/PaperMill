"""source_records/ helpers — enforce separation of metadata and fetch records.

Each paper_raw workspace has a ``source_records/`` directory that holds
external source records. The two record types MUST be kept in separate files:

- ``source_records/metadata_source.<provider>.json`` — CrossRef/OpenAlex/manual
  metadata source records (the authoritative bibliographic raw data).
- ``source_records/fetch_result.json`` — PDF download/fetch result record.

``metadata.source.raw_record_path`` in ``<paper_number>.metadata.json`` must
always point at a ``metadata_source.<provider>.json`` file, NEVER at
``fetch_result.json``. This module centralizes that convention so no script
accidentally overwrites a metadata source record with a fetch result.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from src.utils.atomic_io import atomic_write_json


SOURCE_RECORDS_DIR = "source_records"
FETCH_RESULT_FILENAME = "fetch_result.json"
METADATA_SOURCE_PREFIX = "metadata_source"
_DEFAULT_PROVIDER = "manual"


def _safe_provider(provider: str) -> str:
    """Normalize a provider name for use in a filename (lowercase, no spaces)."""
    cleaned = (str(provider or "")).strip().lower().replace(" ", "_")
    return cleaned or _DEFAULT_PROVIDER


def metadata_source_rel_path(provider: str) -> str:
    """Return the repo-relative POSIX path for a metadata source record."""
    return f"{SOURCE_RECORDS_DIR}/{METADATA_SOURCE_PREFIX}.{_safe_provider(provider)}.json"


def fetch_result_rel_path() -> str:
    """Return the repo-relative POSIX path for the PDF fetch result record."""
    return f"{SOURCE_RECORDS_DIR}/{FETCH_RESULT_FILENAME}"


def manual_metadata_source_record(
    *,
    original_filename: str = "",
    original_path: str = "",
    note: str = "metadata unresolved at staging time",
) -> dict[str, Any]:
    """Build the metadata source record for a manual PDF staging."""
    return {
        "kind": "manual_pdf",
        "original_filename": str(original_filename or ""),
        "original_path": str(original_path or ""),
        "note": note,
    }


def write_metadata_source_record(
    folder: str | Path,
    provider: str,
    record: dict[str, Any],
) -> Path:
    """Write a metadata source record to ``source_records/metadata_source.<provider>.json``.

    Returns the absolute path written. Never writes to ``fetch_result.json``.
    """
    folder = Path(folder)
    rel = metadata_source_rel_path(provider)
    path = folder / rel
    atomic_write_json(path, record, indent=2)
    return path


def write_fetch_result(
    folder: str | Path,
    fetch_record: dict[str, Any],
) -> Path:
    """Write a PDF fetch result record to ``source_records/fetch_result.json``.

    This is the ONLY writer for fetch results. It must never overwrite a
    metadata source record.
    """
    folder = Path(folder)
    rel = fetch_result_rel_path()
    path = folder / rel
    atomic_write_json(path, {"fetch_result": fetch_record}, indent=2)
    return path


def is_fetch_result_path(rel_path: str) -> bool:
    """True when a stored raw_record_path points at the fetch result file."""
    if not rel_path:
        return False
    normalized = str(rel_path).replace("\\", "/").lstrip("./")
    return normalized == fetch_result_rel_path()


def ensure_raw_record_path_is_metadata_source(
    raw_record_path: str,
    provider: str,
) -> str:
    """Return a raw_record_path that is guaranteed NOT to be the fetch_result path.

    If ``raw_record_path`` is empty or points at ``fetch_result.json``, return
    the canonical ``metadata_source.<provider>.json`` path instead. Otherwise
    preserve the caller-supplied path (it may already be a valid metadata
    source path).
    """
    if raw_record_path and not is_fetch_result_path(raw_record_path):
        return raw_record_path
    return metadata_source_rel_path(provider)
