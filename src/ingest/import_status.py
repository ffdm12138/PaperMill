"""Ingest state-machine constants and ``.import_status.json`` helper.

Centralizes the status strings used across the paper_raw → data/papers
ingest pipeline (stage / convert / resolve / curate / formalize / commit)
and provides a single ``write_import_status`` writer so every stage records
the same field shape.

Numeric marker-bearing workspaces route through the nested status-v2 engine
in ``src.ingest.status`` via the translation table below; the flat shape is
written only for non-numeric folders. This facade plus that engine are the
only ``.import_status.json`` writers.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from filelock import FileLock

from src.utils.atomic_io import atomic_write_json_unlocked
from src.utils.timestamps import now_iso


class UnknownLegacyStatusError(ValueError):
    """Raised when a flat legacy status has no registered v2 mapping."""


# --- staging / convert -------------------------------------------------------
READY_FOR_CONVERT = "ready_for_convert"
CONVERTED = "converted"
STALE_CONVERSION = "stale_conversion"
PARTIAL_CONVERSION = "partial_conversion"
STAGE_FAILED = "stage_failed"

# --- metadata resolve (mirror src.metadata_resolve.sidecars.STATUS_*) --------
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
PAPER_NAME_MISMATCH = "paper_name_mismatch"
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



def write_import_status(
    folder: str | Path,
    status: str,
    *,
    reason: str = "",
    errors: list[str] | None = None,
    warnings: list[str] | None = None,
    extra: dict | None = None,
    fsync: bool = True,
) -> dict:
    """Write ``<folder>/.import_status.json`` with the canonical field shape.

    Always includes: status, reason, errors, warnings, created_at, updated_at.
    ``created_at`` is preserved from an existing file (so repeated writes during
    a multi-stage pipeline keep the original timestamp); ``updated_at`` is
    always refreshed. Any keys in ``extra`` (e.g. paper_name, paper_number,
    source_id, pdf_sha256) are merged in alongside the canonical fields.
    """
    import json

    folder = Path(folder)
    # Active numeric workspaces persist only nested status v2.  The returned
    # object still exposes ``status`` for callers being migrated, but that key
    # is deliberately not written to disk.
    if len(folder.name) == 16 and folder.name.isdigit() and (folder / f"{folder.name}.paper.number").exists():
        from src.ingest.status import read_status, update_status
        from src.ingest.workspace import PaperRawWorkspace
        workspace = PaperRawWorkspace.from_path(folder)
        mapping = {
            READY_FOR_CONVERT:("pdf","attached"), CONVERTED:("conversion","complete"), STALE_CONVERSION:("conversion","failed"), PARTIAL_CONVERSION:("conversion","failed"), STAGE_FAILED:("pdf","failed"),
            METADATA_CANDIDATES_FOUND:("metadata","resolving"), METADATA_RESOLVE_FAILED:("metadata","invalid"), METADATA_CANDIDATE_CONFLICT:("metadata","mismatch"), METADATA_MATCHED:("metadata","matched"), METADATA_MANUAL_REVIEW_REQUIRED:("metadata","mismatch"), METADATA_INVALID:("metadata","invalid"), METADATA_UNMATCHED:("metadata","mismatch"), METADATA_INCOMPLETE:("metadata","invalid"),
            "staged_metadata":("metadata","resolved"),
            "metadata_resolved":("metadata","resolved"),
            CATALOG_GENERATION_FAILED:("catalog","invalid"), CATALOG_INVALID:("catalog","invalid"), CATALOG_READY:("catalog","validated"), FORMALIZE_FAILED:("formalization","failed"), PAPER_NAME_MISMATCH:("catalog","invalid"), READY_FOR_COMMIT:("formalization","ready"), COMMIT_FAILED:("commit","failed"), COMMITTED:("commit","imported"), IMPORTED:("commit","imported"),
        }
        try:
            dimension, target = mapping[status]
        except (KeyError, TypeError) as exc:
            raise UnknownLegacyStatusError(
                f"unknown legacy import status {status!r} in {folder} from field 'status'; "
                f"supported values: {', '.join(sorted(mapping))}"
            ) from exc
        fields={"reason":reason,"errors":errors or [],"warnings":warnings or []}
        if extra: fields.update(extra)
        payload=update_status(workspace,dimension,target,fsync=fsync,**fields)
        return {**payload,"status":status}
    path = folder / ".import_status.json"
    lock = FileLock(str(folder / ".import_status.lock"))
    with lock:
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
        atomic_write_json_unlocked(path, payload, indent=2, fsync=fsync)
        return payload


def read_import_status(folder: str | Path) -> dict:
    """Read ``<folder>/.import_status.json``; ``{}`` if absent, ``json_invalid`` on error."""
    folder = Path(folder)
    path = folder / ".import_status.json"
    if not path.exists():
        return {}
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "json_invalid"}
    if not isinstance(data, dict): return {"status":"json_invalid"}
    if data.get("schema_version") == "2.0":
        states={key:(data.get(key) or {}).get("state") for key in ("metadata","pdf","conversion","catalog","formalization","commit")}
        if states["commit"]=="imported": status=IMPORTED
        elif states["commit"]=="failed": status=COMMIT_FAILED
        elif states["formalization"]=="ready": status=READY_FOR_COMMIT
        elif states["formalization"]=="failed": status=FORMALIZE_FAILED
        elif states["catalog"] in {"validated","frozen"}: status=CATALOG_READY
        elif states["catalog"]=="invalid": status=CATALOG_INVALID
        elif states["metadata"] in {"matched","frozen"}: status=METADATA_MATCHED
        elif states["metadata"]=="mismatch": status=METADATA_MANUAL_REVIEW_REQUIRED
        elif states["conversion"]=="complete": status=CONVERTED
        elif states["pdf"]=="attached": status=READY_FOR_CONVERT
        else: status=""
        active={}
        for dimension in ("commit","formalization","catalog","metadata","conversion","pdf"):
            candidate=data.get(dimension) or {}
            if candidate.get("reason") or candidate.get("errors") or candidate.get("warnings"): active=candidate; break
        return {**data,"status":status,"reason":active.get("reason","") ,"errors":active.get("errors",[]),"warnings":active.get("warnings",[]),"created_at":data.get("created_at") or data.get("updated_at")}
    return data
