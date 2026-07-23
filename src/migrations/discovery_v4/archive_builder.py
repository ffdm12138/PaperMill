"""Legacy archive builder for Discovery v4 migration.

Moves v2/v3 pending_pages journals and keyword notebooks to the legacy archive.
Never mutates in-place — reads from source, writes to archive.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import (
    DISCOVERY_KEYWORD_NOTEBOOK_DIR,
    DISCOVERY_LEGACY_ARCHIVE_DIR,
    DISCOVERY_PENDING_PAGES_DIR,
)


def _sha256_hex(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    raw = payload.encode("utf-8")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
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


def archive_pending_pages(
    source_dir: Path,
    archive_root: Path,
    migration_id: str,
) -> dict[str, Any]:
    """Move all pending_pages journals to the legacy archive.

    Returns a manifest with per-file path, size, and SHA-256.
    """
    target_dir = archive_root / "pending_pages"
    manifest_entries: list[dict[str, Any]] = []
    total_size = 0
    file_count = 0

    if source_dir.exists():
        for path in sorted(source_dir.rglob("*.json")):
            if not path.is_file():
                continue
            rel = path.relative_to(source_dir.parent)
            dest = archive_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)

            sha = _sha256_hex(path)
            size = path.stat().st_size
            total_size += size
            file_count += 1

            # Copy with metadata preservation
            shutil.copy2(path, dest)

            # Try to extract metadata from JSON
            schema_ver = "unknown"
            kw_id = "unknown"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    schema_ver = str(data.get("schema_version", "unknown"))
                    kw_id = str(data.get("keyword_id", "unknown"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                pass

            manifest_entries.append({
                "path": str(rel),
                "size": size,
                "sha256": sha,
                "schema_version": schema_ver,
                "keyword_id": kw_id,
            })

    manifest = {
        "migration_id": migration_id,
        "source": str(source_dir),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "total_files": file_count,
        "total_size_bytes": total_size,
        "aggregate_sha256": hashlib.sha256(
            "".join(sorted(e["sha256"] for e in manifest_entries)).encode("utf-8")
        ).hexdigest() if manifest_entries else None,
        "files": manifest_entries,
    }

    manifest_path = target_dir / "archive_manifest.json"
    _write_json_atomic(manifest_path, manifest)

    return manifest


def archive_keyword_notebooks(
    source_dir: Path,
    archive_root: Path,
    migration_id: str,
) -> dict[str, Any]:
    """Copy keyword notebooks to legacy archive (source is kept for config extraction)."""
    target_dir = archive_root / "keyword_notebooks"
    entries: list[dict[str, Any]] = []

    if source_dir.exists():
        for path in sorted(source_dir.glob("*.json")):
            if not path.is_file():
                continue
            dest = target_dir / path.name
            dest.parent.mkdir(parents=True, exist_ok=True)

            sha = _sha256_hex(path)
            size = path.stat().st_size
            shutil.copy2(path, dest)

            entries.append({
                "path": str(path.name),
                "size": size,
                "sha256": sha,
            })

    manifest = {
        "migration_id": migration_id,
        "source": str(source_dir),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "total_files": len(entries),
        "files": entries,
    }

    manifest_path = target_dir / "archive_manifest.json"
    _write_json_atomic(manifest_path, manifest)

    return manifest


def prepare_legacy_archive(migration_id: str) -> dict[str, Any]:
    """Archive all legacy discovery state for the given migration.

    Copies pending_pages journals and keyword notebooks to
    ``data/discovery/legacy_archive/<migration_id>/``.

    Source files are never modified — this is read on source, write to archive.
    """
    archive_root = DISCOVERY_LEGACY_ARCHIVE_DIR / migration_id
    if archive_root.exists():
        raise FileExistsError(f"archive already exists: {archive_root}")

    archive_root.mkdir(parents=True, exist_ok=True)

    pages_manifest = archive_pending_pages(
        DISCOVERY_PENDING_PAGES_DIR, archive_root, migration_id,
    )
    nb_manifest = archive_keyword_notebooks(
        DISCOVERY_KEYWORD_NOTEBOOK_DIR, archive_root, migration_id,
    )

    return {
        "migration_id": migration_id,
        "archive_root": str(archive_root),
        "pending_pages": pages_manifest,
        "keyword_notebooks": nb_manifest,
        "pending_pages_total": pages_manifest["total_files"],
        "notebooks_total": nb_manifest["total_files"],
    }
