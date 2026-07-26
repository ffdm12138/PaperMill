"""Durable publication identity for the active formal-paper projection.

The ledger records lifecycle transitions, while this sidecar records the
published bytes that discovery is allowed to trust.  It is intentionally
small: only identity facts and hashes of the immutable formal closure live
here.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from filelock import FileLock

from config.settings import PROJECT_ROOT
from src.utils.file_fingerprint import compute_sha256
from src.utils.path_utils import normalize_repo_path, resolve_stored_path
from src.utils.atomic_io import atomic_write_json_unlocked


FORMAL_PUBLICATION_STATE_SCHEMA_VERSION = "1.0"
FORMAL_PUBLICATION_STATE_NAME = ".formal_publication_state.json"


@dataclass(frozen=True)
class FormalPublicationValidation:
    """Read-only result for a publication-state validation pass."""

    valid: bool
    generation: str | None
    entries: Mapping[str, Mapping[str, str]]
    issues: tuple[str, ...] = ()
    revision: int | None = None


@dataclass(frozen=True)
class FormalPublicationHeader:
    """Cheap publication-state identity used between staging epochs.

    The header deliberately does not inspect any formal-paper files.  A full
    closure validation is performed when a discovery batch is created and
    whenever this generation/revision token changes.  This keeps the hot
    incremental-refresh path independent of the number of active formals.
    """

    valid: bool
    generation: str | None
    revision: int | None
    issues: tuple[str, ...] = ()


def publication_state_path(papers_dir: str | Path) -> Path:
    return Path(papers_dir) / FORMAL_PUBLICATION_STATE_NAME


def _entry_facts(entries: Mapping[str, Mapping[str, str]]) -> list[dict[str, str]]:
    return [
        {key: str(value) for key, value in sorted(entry.items())}
        for _, entry in sorted(entries.items())
    ]


def publication_generation(entries: Mapping[str, Mapping[str, str]]) -> str:
    payload = json.dumps(
        _entry_facts(entries), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _active_entries(
    items: Mapping[str, Mapping[str, Any]],
    papers_dir: Path,
    *,
    project_root: Path = PROJECT_ROOT,
) -> tuple[dict[str, dict[str, str]], list[str]]:
    entries: dict[str, dict[str, str]] = {}
    issues: list[str] = []
    papers_root = papers_dir.resolve()
    for number, item in sorted(items.items()):
        if not isinstance(item, Mapping) or str(item.get("state") or "") != "active":
            continue
        number = str(number)
        stored_path = str(item.get("folder_path") or "")
        folder = resolve_stored_path(stored_path, project_root=project_root)
        if (
            not stored_path or folder.is_symlink() or not folder.is_dir()
            or folder.parent.resolve() != papers_root
        ):
            issues.append(f"{number}:formal_path_invalid")
            continue
        paper_name = str(item.get("paper_name") or "")
        if not paper_name or paper_name != folder.name:
            issues.append(f"{number}:formal_identity_invalid")
            continue
        manifest = folder / f"{paper_name}.asset_manifest.json"
        metadata = folder / f"{paper_name}.metadata.json"
        catalog = folder / f"{paper_name}.catalog.json"
        if not all(path.is_file() and not path.is_symlink() for path in (manifest, metadata, catalog)):
            issues.append(f"{number}:formal_publication_asset_missing")
            continue
        try:
            entries[number] = {
                "paper_number": number,
                "paper_name": paper_name,
                "folder_path": normalize_repo_path(folder, project_root=project_root),
                "asset_manifest_sha256": compute_sha256(manifest),
                "metadata_sha256": compute_sha256(metadata),
                "catalog_sha256": compute_sha256(catalog),
            }
        except OSError as exc:
            issues.append(f"{number}:formal_publication_hash_failed:{type(exc).__name__}")
    return entries, issues


def _read_state(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    if not path.is_file():
        return None, ["publication_state_missing"]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, [f"publication_state_unreadable:{type(exc).__name__}"]
    if not isinstance(value, dict):
        return None, ["publication_state_not_mapping"]
    return value, []


def read_publication_state_header(
    *, papers_dir: str | Path,
    active_numbers: set[str] | frozenset[str],
    allow_missing_when_empty: bool = True,
) -> FormalPublicationHeader:
    """Read only the durable revision/generation and active-number set.

    This is intentionally a bounded JSON read.  It is safe for every staging
    epoch because it never hashes or opens the manifest, Metadata, Catalog,
    PDF, or any other formal asset.  Call :func:`validate_publication_state`
    for the full hash closure at a batch boundary or after a generation change.
    """
    state, read_issues = _read_state(publication_state_path(papers_dir))
    active_numbers = {str(number) for number in active_numbers}
    if state is None:
        if not active_numbers and allow_missing_when_empty:
            return FormalPublicationHeader(True, None, None, ())
        return FormalPublicationHeader(False, None, None, tuple(read_issues))

    issues = list(read_issues)
    if str(state.get("schema_version") or "") != FORMAL_PUBLICATION_STATE_SCHEMA_VERSION:
        issues.append("publication_state_schema_unsupported")
    entries = state.get("entries")
    if not isinstance(entries, dict):
        issues.append("publication_state_entries_invalid")
        entry_numbers: set[str] = set()
    else:
        entry_numbers = {str(number) for number in entries}
    if entry_numbers != active_numbers:
        issues.append("publication_state_active_set_mismatch")
    generation = str(state.get("generation") or "")
    if not generation:
        issues.append("publication_state_generation_invalid")
    revision = state.get("revision")
    if not isinstance(revision, int) or revision < 1:
        issues.append("publication_state_revision_invalid")
        revision_value: int | None = None
    else:
        revision_value = revision
    return FormalPublicationHeader(
        not issues, generation or None, revision_value, tuple(issues),
    )


def validate_publication_state(
    *, papers_dir: str | Path, ledger_items: Mapping[str, Mapping[str, Any]],
    allow_missing_when_empty: bool = True,
    project_root: Path = PROJECT_ROOT,
) -> FormalPublicationValidation:
    """Validate sidecar identity, active membership, and every bound hash."""
    papers_dir = Path(papers_dir)
    expected, issues = _active_entries(
        ledger_items, papers_dir, project_root=project_root,
    )
    active_numbers = {
        str(number) for number, item in ledger_items.items()
        if isinstance(item, Mapping) and str(item.get("state") or "") == "active"
    }
    state_path = publication_state_path(papers_dir)
    state, read_issues = _read_state(state_path)
    if state is None:
        if not active_numbers and allow_missing_when_empty:
            return FormalPublicationValidation(True, None, {}, tuple(issues), None)
        return FormalPublicationValidation(False, None, {}, tuple(issues + read_issues), None)
    if str(state.get("schema_version") or "") != FORMAL_PUBLICATION_STATE_SCHEMA_VERSION:
        issues.append("publication_state_schema_unsupported")
    entries = state.get("entries")
    if not isinstance(entries, dict):
        issues.append("publication_state_entries_invalid")
        entries = {}
    normalized_entries: dict[str, dict[str, str]] = {}
    for number, entry in entries.items():
        if not isinstance(entry, Mapping):
            issues.append(f"{number}:publication_state_entry_invalid")
            continue
        normalized_entries[str(number)] = {
            str(key): str(value) for key, value in entry.items()
        }
    if set(normalized_entries) != set(expected):
        issues.append("publication_state_active_set_mismatch")
    if normalized_entries != expected:
        issues.append("publication_state_entry_mismatch")
    generation = str(state.get("generation") or "")
    expected_generation = publication_generation(expected)
    if generation != expected_generation:
        issues.append("publication_state_generation_mismatch")
    revision = state.get("revision")
    if not isinstance(revision, int) or revision < 1:
        issues.append("publication_state_revision_invalid")
    return FormalPublicationValidation(
        not issues, generation or None, MappingProxyType(normalized_entries), tuple(issues),
        revision if isinstance(revision, int) and revision >= 1 else None,
    )


def _publish_unlocked(
    *, papers_dir: Path, ledger_items: Mapping[str, Mapping[str, Any]],
    allow_initialize: bool = False,
) -> dict[str, Any]:
    expected, issues = _active_entries(ledger_items, papers_dir)
    if issues:
        raise ValueError(";".join(issues))
    path = publication_state_path(papers_dir)
    previous, previous_issues = _read_state(path)
    active_numbers = {
        str(number) for number, item in ledger_items.items()
        if isinstance(item, Mapping) and str(item.get("state") or "") == "active"
    }
    if previous is None and active_numbers and not allow_initialize:
        raise ValueError("publication_state_missing_for_existing_formals")
    previous_entries = previous.get("entries") if isinstance(previous, dict) else None
    previous_generation = str(previous.get("generation") or "") if isinstance(previous, dict) else ""
    previous_revision = int(previous.get("revision") or 0) if isinstance(previous, dict) else 0
    if previous is not None and previous_issues:
        raise ValueError(";".join(previous_issues))
    generation = publication_generation(expected)
    revision = previous_revision if previous_entries == expected and previous_generation == generation else previous_revision + 1
    if revision < 1:
        revision = 1
    value = {
        "schema_version": FORMAL_PUBLICATION_STATE_SCHEMA_VERSION,
        "revision": revision,
        "generation": generation,
        "entries": expected,
    }
    atomic_write_json_unlocked(path, value, indent=2, sort_keys=True)
    return value


def publish_formal_publication_state(
    *, papers_dir: str | Path, ledger_items: Mapping[str, Mapping[str, Any]],
    allow_initialize: bool = False,
) -> dict[str, Any]:
    """Publish the sidecar under its own ranked publication lock."""
    papers_dir = Path(papers_dir)
    path = publication_state_path(papers_dir)
    with FileLock(str(path) + ".lock"):
        return _publish_unlocked(
            papers_dir=papers_dir, ledger_items=ledger_items,
            allow_initialize=allow_initialize,
        )


def publish_formal_publication_state_unlocked(
    *, papers_dir: str | Path, ledger_items: Mapping[str, Mapping[str, Any]],
    allow_initialize: bool = False,
) -> dict[str, Any]:
    """Publish when the caller already holds the publication lock."""
    return _publish_unlocked(
        papers_dir=Path(papers_dir), ledger_items=ledger_items,
        allow_initialize=allow_initialize,
    )
