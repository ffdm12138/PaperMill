"""Side-file writers and `.import_status.json` status vocabulary.

Intermediate resolver states live in side files (``.import_status.json``,
``<id>.metadata.candidates.json``, ``<id>.metadata.resolve_report.json``,
``<id>.metadata.patch.json``); metadata.json itself is only touched by the
apply step.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from src.utils.atomic_io import atomic_write_json
from src.metadata_resolve.candidates import ResolveReport


# .import_status.json statuses (free-form; metadata_match.status enum unchanged)
STATUS_CANDIDATES_FOUND = "metadata_candidates_found"
STATUS_RESOLVE_FAILED = "metadata_resolve_failed"
STATUS_CANDIDATE_CONFLICT = "metadata_candidate_conflict"
STATUS_MATCHED = "metadata_matched"
STATUS_MANUAL_REVIEW = "metadata_manual_review_required"


# ── Side-file writers ──────────────────────────────────────────────────

def _compact_patch(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            if key in {"metadata_match", "bibtex", "pdf", "content", "notes", "abstract", "keywords"}:
                continue
            if key in {"short_zh", "translated_zh", "raw_record"}:
                continue
            compacted = _compact_patch(child)
            if compacted not in ("", None, [], {}):
                out[key] = compacted
        return out
    if isinstance(value, list):
        out = [_compact_patch(item) for item in value]
        return [item for item in out if item not in ("", None, [], {})]
    if isinstance(value, str):
        return value.strip()
    return value


def write_metadata_patch_json(folder: Path, report: ResolveReport) -> Path | None:
    if not report.best_candidate_id:
        return None
    chosen = next((c for c in report.candidates if c.candidate_id == report.best_candidate_id), None)
    if chosen is None or chosen.decision == "rejected":
        return None
    allowed_top_level = {
        "warnings",
        "title",
        "authors",
        "first_author",
        "year",
        "container",
        "publication",
        "identifiers",
        "links",
        "source",
    }
    patch = {
        key: value
        for key, value in _compact_patch(chosen.patch).items()
        if key in allowed_top_level
    }
    if not patch:
        return None
    path = folder / f"{report.source_id}.metadata.patch.json"
    atomic_write_json(path, patch, indent=2)
    return path


def write_candidates_json(folder: Path, report: ResolveReport) -> Path:
    path = folder / f"{report.source_id}.metadata.candidates.json"
    data = {
        "paper_number": report.source_id,
        "paper_raw_id": report.source_id,
        "generated_at": report.created_at,
        "candidates": [
            {
                "candidate_id": c.candidate_id,
                "doi": c.doi,
                "title": c.title,
                "authors": c.authors,
                "year": c.year,
                "venue": c.venue,
                "source": c.source,
                "confidence": c.confidence,
                "score": c.score,
                "evidence": c.evidence,
                "warnings": c.warnings,
            }
            for c in report.candidates
        ],
        "recommendation": {
            "best_candidate_id": report.best_candidate_id,
            "decision": report.decision,
            "reason": report.reason,
        },
    }
    atomic_write_json(path, data, indent=2)
    return path


def write_resolve_report_json(folder: Path, report: ResolveReport) -> Path:
    path = folder / f"{report.source_id}.metadata.resolve_report.json"
    atomic_write_json(path, report.to_dict(), indent=2)
    return path
