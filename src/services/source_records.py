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


def resolve_metadata_source_record_path(
    folder: Path,
    raw_record_path: str,
) -> tuple[Path | None, str]:
    """Validate and resolve a ``source.raw_record_path`` relative to *folder*.

    Returns ``(resolved_path, error)``.  On success *error* is empty and
    *resolved_path* is the absolute resolved path.  On failure *resolved_path*
    is ``None`` and *error* describes the violation.

    Validation rules:
    1. *raw_record_path* must not be empty.
    2. *raw_record_path* must not be an absolute path.
    3. *raw_record_path* must not escape *folder* via ``..``.
    4. Must reside under ``source_records/``.
    5. Filename must match ``metadata_source.*.json``.
    6. Must NOT be ``source_records/fetch_result.json``.
    """
    if not raw_record_path or not raw_record_path.strip():
        return None, "raw_record_path is empty"
    raw_record_path = raw_record_path.strip()

    raw = Path(raw_record_path)
    if raw.is_absolute():
        return None, f"raw_record_path must not be absolute: {raw_record_path}"

    # Resolve to catch path traversal
    try:
        resolved = (Path(folder) / raw).resolve()
        folder_resolved = Path(folder).resolve()
        resolved.relative_to(folder_resolved)
    except ValueError:
        return None, f"raw_record_path escapes folder: {raw_record_path}"

    # Must be under source_records/
    try:
        rel = resolved.relative_to(folder_resolved)
        parts = rel.parts
    except ValueError:
        return None, f"raw_record_path must be under source_records/: {raw_record_path}"

    if len(parts) < 2 or parts[0] != SOURCE_RECORDS_DIR:
        return None, f"raw_record_path must be under source_records/: {raw_record_path}"

    filename = parts[-1]

    # Reject fetch_result.json
    if filename == FETCH_RESULT_FILENAME:
        return None, f"raw_record_path must not point at {FETCH_RESULT_FILENAME}"

    # Must match metadata_source.*.json
    if not (filename.startswith(f"{METADATA_SOURCE_PREFIX}.") and filename.endswith(".json")):
        return None, (
            f"raw_record_path filename must match {METADATA_SOURCE_PREFIX}.*.json: "
            f"{filename}"
        )

    return resolved, ""


def validate_metadata_source_record_exists(
    folder: Path,
    raw_record_path: str,
    *,
    require_nonempty: bool = False,
) -> list[str]:
    """Validate that *raw_record_path* in *folder* points to an existing file.

    Returns a list of error strings (empty = valid).  Rules:
    - If *require_nonempty* is True, an empty path is an error.
    - Path must be valid (delegates to ``resolve_metadata_source_record_path``).
    - The resolved file must exist.
    """
    if not raw_record_path or not raw_record_path.strip():
        if require_nonempty:
            return ["source.raw_record_path is required"]
        return []  # empty is not an error in non-strict mode

    errors: list[str] = []
    resolved, err = resolve_metadata_source_record_path(folder, raw_record_path)
    if err:
        errors.append(f"source.raw_record_path invalid: {err}")
        return errors

    if not resolved.exists():
        errors.append(
            f"source.raw_record_path points to file that does not exist: "
            f"{raw_record_path}"
        )
    return errors
