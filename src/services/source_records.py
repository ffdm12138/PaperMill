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

import re
import unicodedata
from pathlib import Path
from typing import Any

from src.utils.atomic_io import atomic_write_json


SOURCE_RECORDS_DIR = "source_records"
FETCH_RESULT_FILENAME = "fetch_result.json"
METADATA_SOURCE_PREFIX = "metadata_source"
# Windows reserved filenames (case-insensitive, no extension).
_WINDOWS_RESERVED = frozenset({
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
})


class InvalidProviderIdentityError(ValueError):
    """Raised when a provider name cannot be safely normalized to a filename slug."""
    pass


class SourceRecordPathEscapeError(ValueError):
    """Raised when a resolved source-record path escapes the source_records/ directory."""
    pass


def normalize_provider_slug(provider: str) -> str:
    """Return a safe, deterministic filename slug for a provider identity.

    Rules (in order):
    1. Input must be a non-empty string.
    2. Unicode NFC-normalized.
    3. Strip leading/trailing whitespace.
    4. Lowercased.
    5. Spaces replaced with a single ``_``.
    6. Allow only ``[a-z0-9][a-z0-9._-]{0,63}``.
    7. Collapse consecutive ``_``, ``.``, ``-`` into one.
    8. Must not be empty after cleaning.
    9. Must not be a Windows reserved name (CON, PRN, AUX, NUL, COM[1-9], LPT[1-9]).
    10. Must not start or end with ``.``, ``-``, or ``_``.
    11. Must not contain ``/``, ``\\``, ``..``, ``:``, NUL, or control characters.

    Raises ``InvalidProviderIdentityError`` on any violation.
    """
    if not isinstance(provider, str) or not provider:
        raise InvalidProviderIdentityError("provider must be a non-empty string")

    # Unicode NFC normalization
    slug = unicodedata.normalize("NFC", provider)

    # Reject control chars / NUL
    for ch in slug:
        if ord(ch) < 0x20 or ch == "\x7f":
            raise InvalidProviderIdentityError(
                f"provider contains control character: {ch!r}"
            )

    # Reject path separators and colon
    for forbidden in ("/", "\\", "..", ":"):
        if forbidden in slug:
            raise InvalidProviderIdentityError(
                f"provider contains forbidden sequence: {forbidden!r}"
            )

    slug = slug.strip()
    if not slug:
        raise InvalidProviderIdentityError("provider is empty after stripping")

    slug = slug.lower()

    # Replace spaces with underscore
    slug = slug.replace(" ", "_")

    # Collapse consecutive separators
    slug = re.sub(r"[._-]{2,}", lambda m: m.group(0)[0], slug)

    # Remove leading/trailing dots, hyphens, underscores
    slug = slug.strip("._-")

    if not slug:
        raise InvalidProviderIdentityError("provider is empty after sanitization")

    # Length limit
    if len(slug) > 64:
        raise InvalidProviderIdentityError(
            f"provider slug too long ({len(slug)} chars, max 64)"
        )

    # Character class
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", slug):
        raise InvalidProviderIdentityError(
            f"provider slug contains invalid characters: {slug!r}"
        )

    # Windows reserved name check
    if slug.split(".")[0] in _WINDOWS_RESERVED:
        raise InvalidProviderIdentityError(
            f"provider slug is a Windows reserved name: {slug!r}"
        )

    return slug


def metadata_source_rel_path(provider: str) -> str:
    """Return the repo-relative POSIX path for a metadata source record."""
    return build_metadata_source_rel_path(provider)


def build_metadata_source_rel_path(provider: str) -> str:
    """Build the only canonical Metadata source-record relative path."""
    slug = normalize_provider_slug(provider)
    return f"{SOURCE_RECORDS_DIR}/{METADATA_SOURCE_PREFIX}.{slug}.json"


def validate_metadata_source_rel_path(path: str) -> str:
    """Validate and canonicalize a stored Metadata source-record path."""
    if not isinstance(path, str) or not path or path != path.strip():
        raise SourceRecordPathEscapeError("raw_record_path must be a non-empty canonical string")
    if "\\" in path or path.startswith("/") or ":" in path or ".." in path:
        raise SourceRecordPathEscapeError(f"invalid raw_record_path: {path!r}")
    match = re.fullmatch(r"source_records/metadata_source\.([a-z0-9][a-z0-9._-]{0,63})\.json", path)
    if not match:
        raise SourceRecordPathEscapeError(f"invalid metadata source-record path: {path!r}")
    provider = normalize_provider_slug(match.group(1))
    canonical = build_metadata_source_rel_path(provider)
    if path != canonical:
        raise SourceRecordPathEscapeError(f"non-canonical metadata source-record path: {path!r}")
    return canonical


def resolve_safe_source_record_target(workspace: Path, filename: str) -> Path:
    """Return a direct, non-symlink target inside a real workspace.

    This closes known-path symlink escapes. It does not claim complete
    protection from a hostile process racing filesystem changes.
    """
    workspace = Path(workspace)
    if workspace.is_symlink() or not workspace.exists() or not workspace.is_dir():
        raise SourceRecordPathEscapeError(f"workspace must be an existing real directory: {workspace}")
    workspace_resolved = workspace.resolve(strict=True)
    root = workspace_resolved / SOURCE_RECORDS_DIR
    if root.is_symlink():
        raise SourceRecordPathEscapeError(f"source_records must not be a symlink: {root}")
    if root.exists() and not root.is_dir():
        raise SourceRecordPathEscapeError(f"source_records must be a directory: {root}")
    root.mkdir(exist_ok=True)
    if root.is_symlink():
        raise SourceRecordPathEscapeError(f"source_records became a symlink: {root}")
    root_resolved = root.resolve(strict=True)
    if root_resolved.parent != workspace_resolved:
        raise SourceRecordPathEscapeError(f"source_records escapes workspace: {root_resolved}")
    if Path(filename).name != filename or not filename:
        raise SourceRecordPathEscapeError(f"invalid source-record filename: {filename!r}")
    target = root_resolved / filename
    if target.is_symlink():
        raise SourceRecordPathEscapeError(f"source-record target must not be a symlink: {target}")
    if target.exists() and target.is_dir():
        raise SourceRecordPathEscapeError(f"source-record target must not be a directory: {target}")
    if target.parent.resolve(strict=True) != root_resolved:
        raise SourceRecordPathEscapeError(f"source-record target escapes workspace: {target}")
    return target


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

    Security: validates the resolved path is strictly contained within the
    ``source_records/`` subdirectory of *folder*.  Raises
    ``SourceRecordPathEscapeError`` if the path would escape.
    """
    provider_slug = normalize_provider_slug(provider)
    target = resolve_safe_source_record_target(
        Path(folder), f"{METADATA_SOURCE_PREFIX}.{provider_slug}.json"
    )
    atomic_write_json(target, record, indent=2)
    resolve_safe_source_record_target(Path(folder), target.name)
    return target


def write_fetch_result(
    folder: str | Path,
    fetch_record: dict[str, Any],
) -> Path:
    """Write a PDF fetch result record to ``source_records/fetch_result.json``.

    This is the ONLY writer for fetch results. It must never overwrite a
    metadata source record.
    """
    from src.fetch.pdf_transport import sanitize_for_persistence

    path = resolve_safe_source_record_target(Path(folder), FETCH_RESULT_FILENAME)
    atomic_write_json(path, {"fetch_result": sanitize_for_persistence(fetch_record)}, indent=2)
    resolve_safe_source_record_target(Path(folder), path.name)
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
    if not raw_record_path or is_fetch_result_path(raw_record_path):
        return build_metadata_source_rel_path(provider)
    return validate_metadata_source_rel_path(raw_record_path)


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
