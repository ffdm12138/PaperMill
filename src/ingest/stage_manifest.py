"""Unified stage_manifest.json writer for paper_raw workspaces.

Both ingest paths (manual PDF and DOI-first network metadata + PDF fetch) must
produce the same stage_manifest schema so downstream tooling can treat them
uniformly. The schema nests source/staged details under ``pdf_source`` and
``staged_pdf``:

{
  "schema_version": "1.0",
  "paper_number": "...",
  "paper_raw_id": "...",
  "workflow_path": "manual_pdf | network_metadata | network_metadata_pdf_fetch",
  "source_type": "manual_pdf | network_search",
  "pdf_source": {...} | null,
  "staged_pdf": {...} | null,
  "created_at": "...",
  "updated_at": "..."
}

The manual PDF path sets ``pdf_source.kind = "local_raw_queue"``; the DOI-first
fetch path sets ``pdf_source.kind = "doi_fetch"``. Network metadata staged
before PDF fetch writes ``pdf_source = null`` and ``staged_pdf = null``.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from src.utils.atomic_io import atomic_write_json
from src.utils.timestamps import now_iso as _now_iso


STAGE_MANIFEST_VERSION = "1.0"

WORKFLOW_MANUAL_PDF = "manual_pdf"
WORKFLOW_NETWORK_METADATA = "network_metadata"
WORKFLOW_NETWORK_METADATA_PDF_FETCH = "network_metadata_pdf_fetch"

VALID_WORKFLOW_PATHS = {
    WORKFLOW_MANUAL_PDF,
    WORKFLOW_NETWORK_METADATA,
    WORKFLOW_NETWORK_METADATA_PDF_FETCH,
}



def read_stage_manifest(folder: str | Path) -> dict[str, Any]:
    """Read ``stage_manifest.json``; ``{}`` if absent or invalid."""
    path = Path(folder) / "stage_manifest.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _staged_pdf_entry(dest_pdf: Path, *, rel_path: str, hashes: dict | None = None) -> dict[str, Any]:
    if hashes is None:
        return {"path": rel_path}
    return {
        "path": rel_path,
        "md5": hashes.get("md5", ""),
        "sha256": hashes.get("sha256", ""),
        "file_size": hashes.get("file_size", 0),
    }


def manual_pdf_source(
    *,
    operation: str,
    original_path: str,
    original_filename: str,
    original_hashes: dict,
) -> dict[str, Any]:
    """Build the ``pdf_source`` dict for the manual PDF (local raw queue) path."""
    return {
        "kind": "local_raw_queue",
        "operation": operation,
        "original_path": str(original_path),
        "original_filename": str(original_filename),
        "original_md5": original_hashes.get("md5", ""),
        "original_sha256": original_hashes.get("sha256", ""),
        "original_file_size": original_hashes.get("file_size", 0),
    }


def doi_fetch_pdf_source(
    *,
    operation: str = "attach",
    fetch_record_path: str = "source_records/fetch_result.json",
    resolver: str = "",
    pdf_url: str = "",
    doi: str = "",
) -> dict[str, Any]:
    """Build the ``pdf_source`` dict for the DOI-first PDF fetch path."""
    from src.fetch.pdf_transport import sanitize_url_for_persistence

    return {
        "kind": "doi_fetch",
        "operation": operation,
        "fetch_record_path": fetch_record_path,
        "resolver": str(resolver or ""),
        "pdf_url": sanitize_url_for_persistence(str(pdf_url or "")),
        "doi": str(doi or ""),
    }


def write_stage_manifest(
    folder: str | Path,
    *,
    paper_number: str,
    paper_raw_id: str = "",
    workflow_path: str,
    source_type: str,
    pdf_source: dict[str, Any] | None = None,
    staged_pdf: dict[str, Any] | None = None,
    fsync: bool = True,
) -> dict[str, Any]:
    """Write a unified ``stage_manifest.json``.

    Preserves ``created_at`` if the manifest already exists (idempotent update).
    """
    folder = Path(folder)
    if workflow_path not in VALID_WORKFLOW_PATHS:
        raise ValueError(f"invalid workflow_path: {workflow_path}")
    existing = read_stage_manifest(folder)
    created_at = existing.get("created_at") or _now_iso()
    updated_at = _now_iso()
    manifest: dict[str, Any] = {
        "schema_version": STAGE_MANIFEST_VERSION,
        "paper_number": paper_number,
        "paper_raw_id": paper_raw_id or paper_number,
        "workflow_path": workflow_path,
        "source_type": source_type,
        "pdf_source": pdf_source,
        "staged_pdf": staged_pdf,
        "created_at": created_at,
        "updated_at": updated_at,
    }
    atomic_write_json(folder / "stage_manifest.json", manifest, indent=2, fsync=fsync)
    return manifest


def update_stage_manifest(
    folder: str | Path,
    *,
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Merge ``updates`` into the existing stage_manifest (preserving schema_version).

    Used by ``attach_pdf`` to fill in ``staged_pdf`` / ``pdf_source`` after a
    fetch, without losing the ``created_at`` / ``workflow_path`` set at staging.
    """
    folder = Path(folder)
    existing = read_stage_manifest(folder)
    existing.update(updates)
    existing["schema_version"] = STAGE_MANIFEST_VERSION
    existing["updated_at"] = _now_iso()
    if "created_at" not in existing:
        existing["created_at"] = _now_iso()
    atomic_write_json(folder / "stage_manifest.json", existing, indent=2)
    return existing


def staged_pdf_hashes(manifest: dict[str, Any]) -> tuple[str, str]:
    """Extract (md5, sha256) from a manifest's ``staged_pdf`` entry."""
    staged = manifest.get("staged_pdf") if isinstance(manifest.get("staged_pdf"), dict) else {}
    return str(staged.get("md5") or ""), str(staged.get("sha256") or "")
