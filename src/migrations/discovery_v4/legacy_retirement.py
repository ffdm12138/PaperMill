"""Retire legacy flat discovery sources after a finalized v4 migration.

The migration's ``legacy_cleaned`` state only ever removed the migration
archive copy and the drained transitional pending store — the original
flat legacy roots (``data/discovery/keyword_notebooks`` and
``data/discovery/pending_pages``) stayed live.  This module performs the
real retirement: gated on a closed post-cutover reconciliation, the flat
roots are moved into ``data/discovery/legacy_retained/<migration_id>/``
with byte-for-byte verification, marked read-only, and replaced by
tombstone files that fail loudly when anything tries to recreate or write
the retired paths.

Purge is a separate, higher-bar operation: it requires the retention
window to have elapsed, an explicit migration-id confirmation, a matching
active generation pointer, and tombstones that still belong to this
migration.  Nothing here deletes anything before those gates pass.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class LegacyRetirementError(RuntimeError):
    """A retirement or purge gate failed; no (further) mutation occurred."""


RETENTION_MANIFEST_SCHEMA = "1.0"
TOMBSTONE_SCHEMA = "1.0"
DEFAULT_RETENTION_DAYS = 90


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_directory_manifest(root: Path) -> dict[str, Any]:
    """Deterministic manifest of a directory tree.

    ``aggregate_sha256`` covers the sorted sequence of
    (relative posix path, file sha256, size) so it is order-independent
    and content-bound.  Streaming reads only.
    """
    root = Path(root)
    if not root.is_dir():
        raise LegacyRetirementError(f"not a directory: {root}")
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        digest = _sha256_file(path)
        size = path.stat().st_size
        entries.append({"path": rel, "sha256": digest, "size": size})
    aggregate = hashlib.sha256()
    for entry in entries:
        aggregate.update(
            json.dumps(entry, sort_keys=True).encode("utf-8") + b"\n"
        )
    return {
        "file_count": len(entries),
        "total_bytes": sum(e["size"] for e in entries),
        "aggregate_sha256": aggregate.hexdigest(),
        "files": entries,
    }


def _manifest_matches(source: dict[str, Any], moved: dict[str, Any]) -> bool:
    return (
        source["file_count"] == moved["file_count"]
        and source["total_bytes"] == moved["total_bytes"]
        and source["aggregate_sha256"] == moved["aggregate_sha256"]
    )


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        try:
            mode = path.stat().st_mode
            path.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        except OSError:
            pass
    try:
        mode = root.stat().st_mode
        root.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    except OSError:
        pass


def _make_writable(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        try:
            path.chmod(path.stat().st_mode | stat.S_IWUSR)
        except OSError:
            pass
    try:
        root.chmod(root.stat().st_mode | stat.S_IWUSR)
    except OSError:
        pass


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        with tmp.open("wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(path))
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _write_tombstone(original_dir: Path, *, migration_id: str,
                     retained_at: Path, retired_at: str) -> None:
    """Replace a retired directory path with a loud tombstone FILE.

    A file at the original directory path makes any ``mkdir(exist_ok=True)``
    or write-through attempt fail immediately instead of silently
    resurrecting the retired layout.
    """
    if original_dir.exists():
        raise LegacyRetirementError(
            f"tombstone target still exists: {original_dir}"
        )
    _write_json_atomic(original_dir, {
        "schema_version": TOMBSTONE_SCHEMA,
        "retired": True,
        "migration_id": migration_id,
        "retained_at": str(retained_at),
        "retired_at": retired_at,
        "message": (
            "This legacy flat discovery directory was retired. Read the "
            "retained copy at retained_at; never recreate this path."
        ),
    })


def _load_reconciliation(report_path: Path, migration_id: str) -> dict[str, Any]:
    if not report_path.is_file():
        raise LegacyRetirementError(
            f"post-cutover reconciliation report missing: {report_path}"
        )
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        raise LegacyRetirementError(
            f"reconciliation report unreadable: {report_path}: {exc}"
        ) from exc
    if report.get("migration_id") != migration_id:
        raise LegacyRetirementError(
            f"reconciliation report migration_id {report.get('migration_id')!r} "
            f"does not match {migration_id!r}"
        )
    if int(report.get("unresolved_items", -1)) != 0:
        raise LegacyRetirementError(
            f"reconciliation is not closed: unresolved_items="
            f"{report.get('unresolved_items')}"
        )
    return report


def retire_legacy_sources(
    *,
    migration_id: str,
    flat_notebooks_dir: Path,
    flat_pending_pages_dir: Path,
    retained_root: Path,
    reconciliation_report_path: Path,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> dict[str, Any]:
    """Move the legacy flat roots into verified read-only retention."""
    flat_notebooks_dir = Path(flat_notebooks_dir)
    flat_pending_pages_dir = Path(flat_pending_pages_dir)
    retained_root = Path(retained_root)
    target = retained_root / migration_id

    report = _load_reconciliation(
        Path(reconciliation_report_path), migration_id
    )
    receipts_dir = (
        Path(reconciliation_report_path).parent / f"{migration_id}.receipts"
    )
    if not receipts_dir.is_dir():
        raise LegacyRetirementError(
            f"receipts directory missing: {receipts_dir}"
        )
    receipt_count = sum(1 for _ in receipts_dir.glob("*.json"))
    verified = int(report.get("receipts_verified", -1))
    if receipt_count != verified:
        raise LegacyRetirementError(
            f"receipt count mismatch: {receipt_count} on disk vs "
            f"{verified} verified in the reconciliation report"
        )
    for flat in (flat_notebooks_dir, flat_pending_pages_dir):
        if not flat.is_dir():
            raise LegacyRetirementError(f"flat directory missing: {flat}")
    if target.exists():
        raise LegacyRetirementError(f"retention target already exists: {target}")

    notebooks_manifest = compute_directory_manifest(flat_notebooks_dir)
    pages_manifest = compute_directory_manifest(flat_pending_pages_dir)

    target.parent.mkdir(parents=True, exist_ok=True)
    moved: list[tuple[Path, Path]] = []
    try:
        shutil.move(str(flat_notebooks_dir), str(target / "keyword_notebooks"))
        moved.append((target / "keyword_notebooks", flat_notebooks_dir))
        shutil.move(str(flat_pending_pages_dir), str(target / "pending_pages"))
        moved.append((target / "pending_pages", flat_pending_pages_dir))
    except Exception as exc:
        # Best-effort rollback so the pair never ends up half-moved.
        for dst, src in reversed(moved):
            try:
                shutil.move(str(dst), str(src))
            except Exception:
                pass
        raise LegacyRetirementError(
            f"failed moving legacy sources into retention: {exc}"
        ) from exc

    moved_notebooks = compute_directory_manifest(target / "keyword_notebooks")
    moved_pages = compute_directory_manifest(target / "pending_pages")
    if not _manifest_matches(notebooks_manifest, moved_notebooks):
        raise LegacyRetirementError(
            "post-move verification failed for keyword_notebooks: "
            "manifest mismatch (data left in retention target for inspection)"
        )
    if not _manifest_matches(pages_manifest, moved_pages):
        raise LegacyRetirementError(
            "post-move verification failed for pending_pages: "
            "manifest mismatch (data left in retention target for inspection)"
        )

    retired_dt = datetime.now(timezone.utc)
    retired_at = retired_dt.isoformat()
    purge_not_before = (retired_dt + timedelta(days=retention_days)).isoformat()
    retention_manifest = {
        "schema_version": RETENTION_MANIFEST_SCHEMA,
        "migration_id": migration_id,
        "retired_at": retired_at,
        "retention_days": retention_days,
        "purge_not_before": purge_not_before,
        "source_paths": {
            "keyword_notebooks": str(flat_notebooks_dir),
            "pending_pages": str(flat_pending_pages_dir),
        },
        "manifests": {
            "keyword_notebooks": moved_notebooks,
            "pending_pages": moved_pages,
        },
    }
    _write_json_atomic(target / "retention_manifest.json", retention_manifest)
    _make_read_only(target)

    _write_tombstone(
        flat_notebooks_dir, migration_id=migration_id,
        retained_at=target / "keyword_notebooks", retired_at=retired_at,
    )
    _write_tombstone(
        flat_pending_pages_dir, migration_id=migration_id,
        retained_at=target / "pending_pages", retired_at=retired_at,
    )

    return {
        "migration_id": migration_id,
        "retired_at": retired_at,
        "retained_root": str(target),
        "purge_not_before": purge_not_before,
        "manifests": {
            "keyword_notebooks": {
                "file_count": moved_notebooks["file_count"],
                "total_bytes": moved_notebooks["total_bytes"],
                "aggregate_sha256": moved_notebooks["aggregate_sha256"],
            },
            "pending_pages": {
                "file_count": moved_pages["file_count"],
                "total_bytes": moved_pages["total_bytes"],
                "aggregate_sha256": moved_pages["aggregate_sha256"],
            },
        },
        "tombstones": [str(flat_notebooks_dir), str(flat_pending_pages_dir)],
    }


def _validate_tombstone(path: Path, migration_id: str) -> None:
    """The tombstone at a retired flat path must still belong to us."""
    if not path.is_file():
        raise LegacyRetirementError(
            f"tombstone missing at retired flat path: {path}"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        raise LegacyRetirementError(
            f"tombstone unreadable at {path}: {exc}"
        ) from exc
    if not data.get("retired") or data.get("migration_id") != migration_id:
        raise LegacyRetirementError(
            f"tombstone at {path} belongs to "
            f"{data.get('migration_id')!r}, not {migration_id!r}"
        )


def purge_retained_legacy(
    *,
    migration_id: str,
    retained_root: Path,
    confirm_migration_id: str,
    now: datetime,
    active_generation_path: Path,
) -> dict[str, Any]:
    """Permanently delete a retained legacy tree after its retention window."""
    if confirm_migration_id != migration_id:
        raise LegacyRetirementError(
            f"purge requires --confirm-migration-id {migration_id!r}, "
            f"got {confirm_migration_id!r}"
        )
    target = Path(retained_root) / migration_id
    manifest_path = target / "retention_manifest.json"
    if not manifest_path.is_file():
        raise LegacyRetirementError(
            f"retention manifest missing: {manifest_path}"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        raise LegacyRetirementError(
            f"retention manifest unreadable: {manifest_path}: {exc}"
        ) from exc
    if manifest.get("migration_id") != migration_id:
        raise LegacyRetirementError(
            f"retention manifest migration_id {manifest.get('migration_id')!r} "
            f"does not match {migration_id!r}"
        )
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    purge_not_before = datetime.fromisoformat(str(manifest["purge_not_before"]))
    if purge_not_before.tzinfo is None:
        purge_not_before = purge_not_before.replace(tzinfo=timezone.utc)
    if now < purge_not_before:
        raise LegacyRetirementError(
            f"retention window still open until {purge_not_before.isoformat()}"
        )
    pointer_path = Path(active_generation_path)
    if pointer_path.is_file():
        try:
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            pointer = {}
        if pointer.get("migration_id") != migration_id:
            raise LegacyRetirementError(
                "active generation pointer does not belong to migration "
                f"{migration_id!r}; refusing purge"
            )

    source_paths = manifest.get("source_paths") or {}
    tombstones = [Path(p) for p in source_paths.values()]
    for tombstone in tombstones:
        _validate_tombstone(tombstone, migration_id)

    _make_writable(target)
    shutil.rmtree(target)
    removed_tombstones: list[str] = []
    for tombstone in tombstones:
        if tombstone.is_file():
            tombstone.unlink()
            removed_tombstones.append(str(tombstone))
    return {
        "migration_id": migration_id,
        "purged_at": now.isoformat(),
        "purged_tree": str(target),
        "tombstones_removed": removed_tombstones,
    }
