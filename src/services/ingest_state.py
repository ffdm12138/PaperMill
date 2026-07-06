"""Ingest state-machine constants and ``.import_status.json`` helper.

Centralizes the status strings used across the paper_raw → data/papers
ingest pipeline (stage / convert / resolve / curate / formalize / commit)
and provides a single ``write_import_status`` writer so every stage records
the same field shape.

This module deliberately depends only on ``src.utils.atomic_io`` (and a
local ``now_iso``) to avoid importing ``v2_library`` (which would create a
cycle, since v2_library imports these constants). The status *strings* are
duplicated here by value; keep them in sync with the literal writers in
v2_library / metadata_resolver until those are migrated.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.utils.atomic_io import atomic_write_json


# --- staging / convert -------------------------------------------------------
READY_FOR_CONVERT = "ready_for_convert"
CONVERTED = "converted"
STALE_CONVERSION = "stale_conversion"
PARTIAL_CONVERSION = "partial_conversion"
STAGE_FAILED = "stage_failed"

# --- metadata resolve (mirror metadata_resolver.STATUS_*) --------------------
METADATA_CANDIDATES_FOUND = "metadata_candidates_found"
METADATA_RESOLVE_FAILED = "metadata_resolve_failed"
METADATA_CANDIDATE_CONFLICT = "metadata_candidate_conflict"
METADATA_MATCHED = "metadata_matched"
METADATA_MANUAL_REVIEW_REQUIRED = "metadata_manual_review_required"
METADATA_INVALID = "metadata_invalid"
METADATA_UNMATCHED = "metadata_unmatched"
METADATA_INCOMPLETE = "metadata_incomplete"

# --- curate / catalog --------------------------------------------------------
CATALOG_GENERATION_FAILED = "catalog_generation_failed"
CATALOG_INVALID = "catalog_invalid"
CATALOG_READY = "catalog_ready"

# --- formalize ---------------------------------------------------------------
FORMALIZE_FAILED = "formalize_failed"
PAPER_ID_MISMATCH = "paper_id_mismatch"
READY_FOR_COMMIT = "ready_for_commit"

# --- commit ------------------------------------------------------------------
POSSIBLE_DUPLICATE = "possible_duplicate"
METADATA_WARNINGS = "metadata_warnings"
COMMIT_FAILED = "commit_failed"
COMMITTED = "committed"
IMPORTED = "imported"

# legacy alias kept so old status files remain readable (no longer written)
COMMIT_POSTCHECK_FAILED = "commit_postcheck_failed"


# Statuses that ``commit_paper_raw_to_papers.py`` accepts as install-ready.
TERMINAL_READY_STATUSES = {READY_FOR_COMMIT}

# Statuses that indicate a paper_raw folder is parked mid-pipeline and may
# need attention (surfaced by directory hygiene checks).
STUCK_HYGIENE_STATUSES = {
    CATALOG_READY,
    FORMALIZE_FAILED,
    COMMIT_FAILED,
    METADATA_CANDIDATES_FOUND,
    METADATA_CANDIDATE_CONFLICT,
    METADATA_MANUAL_REVIEW_REQUIRED,
    METADATA_RESOLVE_FAILED,
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def write_import_status(
    folder: str | Path,
    status: str,
    *,
    reason: str = "",
    errors: list[str] | None = None,
    warnings: list[str] | None = None,
    extra: dict | None = None,
) -> dict:
    """Write ``<folder>/.import_status.json`` with the canonical field shape.

    Always includes: status, reason, errors, warnings, created_at, updated_at.
    ``created_at`` is preserved from an existing file (so repeated writes during
    a multi-stage pipeline keep the original timestamp); ``updated_at`` is
    always refreshed. Any keys in ``extra`` (e.g. paper_id, paper_number,
    source_id, pdf_sha256) are merged in alongside the canonical fields.
    """
    import json

    folder = Path(folder)
    path = folder / ".import_status.json"
    existing: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                existing = data
        except Exception:
            existing = {}
    created_at = existing.get("created_at") or now_iso()
    payload: dict = {
        "status": status,
        "reason": reason,
        "errors": errors or [],
        "warnings": warnings or [],
        "created_at": created_at,
        "updated_at": now_iso(),
    }
    if extra:
        for key, value in extra.items():
            payload[key] = value
    atomic_write_json(path, payload, indent=2)
    return payload


def read_import_status(folder: str | Path) -> dict:
    """Read ``<folder>/.import_status.json``; ``{}`` if absent, ``json_invalid`` on error."""
    path = Path(folder) / ".import_status.json"
    if not path.exists():
        return {}
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "json_invalid"}
    return data if isinstance(data, dict) else {"status": "json_invalid"}
