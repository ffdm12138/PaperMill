"""Conversion-stage gates for paper_raw workspaces (batch converter policy).

Owns the import_status vocabularies and the per-workspace predicates that the
batch conversion entry points report against: which statuses block
re-conversion, which recommend a post-conversion metadata-resolution pass,
whether the physical assets required for conversion are present, and whether
metadata is ready for formalize/commit.  Conversion itself never requires
matched metadata — only the metadata.json shell (staging/import-owned
workspace contract).
"""
from __future__ import annotations

import json
from pathlib import Path

from src.metadata.freeze import assert_metadata_frozen
from src.metadata.quality import is_valid_normalized_doi
from src.metadata.schema import metadata_doi
from src.utils.identifiers import normalize_doi

# import_status values that mean a workspace is past the conversion stage and
# must NOT be re-converted: it is at formalize/commit stage or parked as a
# duplicate. Metadata-incomplete bootstrap statuses (doi_invalid,
# metadata_resolve_failed, metadata_manual_review_required, metadata_unmatched,
# metadata_candidates_found, metadata_candidate_conflict) are intentionally NOT
# here — conversion is allowed before metadata is matched, but a metadata.json
# shell must already exist so staging/import owns workspace initialization.
CONVERSION_BLOCKED_STATUSES = {
    "ready_for_commit",
    "catalog_ready",
    "committed",
    "imported",
    "possible_duplicate",
    "quarantined_duplicate",
}

# import_status values that signal metadata is not yet resolved, so a
# post-conversion metadata-resolution pass is recommended (the converted
# Markdown is a fresh evidence source).
POST_CONVERSION_RESOLVE_RECOMMENDED_STATUSES = {
    "doi_invalid",
    "metadata_resolve_failed",
    "metadata_manual_review_required",
    "metadata_unmatched",
    "metadata_candidates_found",
    "metadata_candidate_conflict",
    "metadata_incomplete",
    "metadata_invalid",
}


def preflight_status(root: Path, source_id: str) -> str:
    """Best-effort read of the workspace ``.import_status.json`` status value."""
    path = root / source_id / ".import_status.json"
    if not path.exists():
        return ""
    try:
        return str((json.loads(path.read_text(encoding="utf-8")) or {}).get("status") or "")
    except Exception:
        return ""


def read_metadata(root: Path, source_id: str) -> dict:
    """Best-effort read of <source_id>.metadata.json; {} if missing/invalid."""
    path = root / source_id / f"{source_id}.metadata.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def metadata_ready_for_commit(root: Path, source_id: str, metadata: dict) -> bool:
    """True only when metadata is matched/manual_confirmed AND DOI is valid.

    This is the formalize/commit gate. Conversion does NOT require this — this
    field is reported per-item so operators can see which workspaces are still
    blocked for formal ingestion after conversion.
    """
    if not metadata:
        return False
    try:
        assert_metadata_frozen(root/source_id,source_id)
    except Exception:
        return False
    doi = normalize_doi(metadata_doi(metadata))
    return bool(doi) and is_valid_normalized_doi(doi)


def metadata_fields_for_report(root: Path, source_id: str) -> dict:
    """Per-item metadata-status fields for the conversion report."""
    import_status = preflight_status(root, source_id)
    metadata = read_metadata(root, source_id)
    return {
        "import_status": import_status,
        "metadata_ready_for_commit": metadata_ready_for_commit(root,source_id,metadata),
        # Conversion only requires the metadata *shell* (staging/import-owned
        # workspace contract), NOT complete matched metadata. DOI/journal/pages
        # are required only at formalize/commit time. See docs/PROJECT_CONTRACT.md
        # "Ingest layered semantics".
        "metadata_shell_required_for_conversion": True,
        "matched_metadata_required_for_conversion": False,
        "post_conversion_metadata_resolution_recommended": (
            import_status in POST_CONVERSION_RESOLVE_RECOMMENDED_STATUSES
            or not metadata_ready_for_commit(root, source_id, metadata)
        ),
    }


def conversion_asset_gate(root: Path, source_id: str) -> tuple[bool, str, bool, bool]:
    """Check the physical assets required for conversion.

    Returns ``(ok, reason, has_pdf, has_metadata_shell)``. Conversion requires
    both the PDF and the metadata.json shell. The shell does not need matched
    DOI metadata yet; it is the staging/import-owned workspace contract that
    later metadata resolution and formalize/commit build on.
    """
    folder = root / source_id
    if not folder.exists():
        return False, "paper_raw workspace missing", False, False
    pdf = folder / f"{source_id}.pdf"
    has_pdf = pdf.exists() and pdf.is_file()
    meta = folder / f"{source_id}.metadata.json"
    has_metadata_shell = meta.exists() and meta.is_file()
    if not has_pdf:
        return False, "missing paper_raw PDF", has_pdf, has_metadata_shell
    if not has_metadata_shell:
        return (
            False,
            "missing metadata.json shell; run staging/import metadata initialization first",
            has_pdf,
            has_metadata_shell,
        )
    return True, "", has_pdf, has_metadata_shell
