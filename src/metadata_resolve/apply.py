"""Apply a resolved candidate to metadata.json under the paper_raw write lock.

The deterministic candidate-selection apply step never writes embedded match
state or the authoritative match receipt. It fills ONLY empty bibliographic
fields (via ``merge_missing_metadata``); non-empty metadata fields are never
overwritten, and identifier conflicts cannot be overridden by candidate
selection or ordinary manual confirmation.
"""
from __future__ import annotations

from pathlib import Path

from config.settings import PAPERS_DIR
from src.ingest.import_status import write_import_status
from src.ingest.locking import paper_raw_write_lock
from src.metadata.normalization import merge_missing_metadata
from src.metadata.quality import bibliographic_identity_gate
from src.metadata.schema import metadata_doi, validate_metadata_schema
from src.metadata.source_records import write_metadata_source_record
from src.utils.atomic_io import atomic_write_json
from src.utils.identifiers import normalize_doi
from src.utils.jsonio import read_json
from src.metadata_resolve.candidates import (
    ResolvedCandidate,
    ResolveReport,
    _duplicate_candidate_reasons,
    _duplicate_pdf_reasons,
)
from src.metadata_resolve.sidecars import STATUS_MANUAL_REVIEW, STATUS_RESOLVE_FAILED




def _has_required_metadata_fields(metadata: dict) -> bool:
    doi = ((metadata.get("identifiers") or {}).get("doi") or "").strip()
    title = ((metadata.get("title") or {}).get("original") or "").strip()
    year = metadata.get("year")
    authors = metadata.get("authors") or []
    has_author = any((a.get("full_name") or a.get("family")) for a in authors if isinstance(a, dict))
    return bool(doi and title and year and has_author)


def _has_venue(metadata: dict) -> bool:
    container = metadata.get("container") or {}
    return any(str(container.get(k) or "").strip() for k in ("journal", "conference", "booktitle"))


def apply_resolution(
    folder: str | Path,
    report: ResolveReport,
    *,
    manual_confirm: bool = False,
    candidate_id: str | None = None,
    papers_dir: str | Path = PAPERS_DIR,
    paper_raw_dir: str | Path | None = None,
) -> dict:
    folder_path = Path(folder)
    paper_raw_root = Path(paper_raw_dir) if paper_raw_dir is not None else folder_path.parent
    with paper_raw_write_lock(paper_raw_root):
        return _apply_resolution_unlocked(
            folder_path,
            report,
            manual_confirm=manual_confirm,
            candidate_id=candidate_id,
            papers_dir=papers_dir,
            paper_raw_dir=paper_raw_root,
        )


def _apply_resolution_unlocked(
    folder: str | Path,
    report: ResolveReport,
    *,
    manual_confirm: bool = False,
    candidate_id: str | None = None,
    papers_dir: str | Path = PAPERS_DIR,
    paper_raw_dir: str | Path | None = None,
) -> dict:
    """Apply a resolved candidate to metadata.json. Returns a result dict.

    - candidate selection writes citation metadata once but does not assert a
      PDF match; the independent receipt stage owns that decision.
    - --manual-confirm selects a candidate only after passing the full
      DOI/dupe(DOI+sha)/conflict/completeness/no-overwrite gate. It relaxes ONLY
      the auto-score threshold, never the validation checks.
    """
    folder = Path(folder)
    source_id = folder.name
    meta_path = folder / f"{source_id}.metadata.json"
    metadata = read_json(meta_path, {})
    paper_raw_root = Path(paper_raw_dir) if paper_raw_dir is not None else folder.parent
    papers_root = Path(papers_dir)

    # choose candidate
    chosen: ResolvedCandidate | None = None
    if candidate_id:
        for c in report.candidates:
            if c.candidate_id == candidate_id:
                chosen = c
                break
        if chosen is None:
            raise ValueError(f"candidate_id {candidate_id!r} not found among report candidates")
    else:
        for c in report.candidates:
            if c.candidate_id == report.best_candidate_id:
                chosen = c
                break
    if chosen is None or not chosen.doi:
        candidate_warnings = list(dict.fromkeys(
            reason
            for candidate in report.candidates
            for reason in candidate.gate_reasons
        ))
        write_import_status(folder, STATUS_MANUAL_REVIEW, reason="no DOI-bearing candidate to apply")
        return {"applied": False, "status": "no_candidate", "paper_number": source_id, "paper_raw_id": source_id,
                "chosen_candidate_id": candidate_id or report.best_candidate_id,
                "warnings": candidate_warnings or ["no DOI-bearing candidate"]}

    existing_doi = metadata_doi(metadata)

    # ── Full validation gate (applies to BOTH auto and manual-confirm) ──
    fail_reasons: list[str] = []
    if "/" not in chosen.doi:
        fail_reasons.append("doi malformed")
    fail_reasons.extend(_duplicate_candidate_reasons(
        chosen.doi,
        paper_raw_dir=paper_raw_root,
        papers_dir=papers_root,
        skip_paper_number=source_id,
    ))
    fail_reasons.extend(_duplicate_pdf_reasons(
        folder / f"{source_id}.pdf",
        paper_raw_dir=paper_raw_root,
        papers_dir=papers_root,
        skip_paper_number=source_id,
    ))
    if existing_doi and normalize_doi(existing_doi) != normalize_doi(chosen.doi):
        fail_reasons.append(f"doi conflict: existing {existing_doi} vs candidate {chosen.doi}")

    # merge first (fills only empties) so we can check completeness on merged data
    merged, merge_warnings = merge_missing_metadata(metadata, chosen.patch)
    gate_ready, gate_reasons = bibliographic_identity_gate(merged, fail_reasons)
    fail_reasons = [] if gate_ready else gate_reasons
    can_auto = chosen.decision == "auto_matched"
    if fail_reasons or (not can_auto and not manual_confirm):
        status = STATUS_MANUAL_REVIEW if chosen.doi else STATUS_RESOLVE_FAILED
        reason = "; ".join(fail_reasons) if fail_reasons else (
            "candidate not auto-matched and --manual-confirm not given"
        )
        write_import_status(folder, status, reason=reason)
        return {"applied": False, "status": "manual_review_required", "paper_number": source_id, "paper_raw_id": source_id,
                "chosen_candidate_id": chosen.candidate_id, "warnings": fail_reasons or [reason]}

    # ── Write ──
    new_status = "resolved"
    merged.pop("metadata_match", None)
    schema_errors = validate_metadata_schema(merged)
    if schema_errors:
        write_import_status(folder, STATUS_MANUAL_REVIEW, reason="; ".join(schema_errors))
        return {"applied": False, "status": "schema_error", "paper_number": source_id, "paper_raw_id": source_id,
                "chosen_candidate_id": chosen.candidate_id, "warnings": schema_errors}

    source = merged.get("source") if isinstance(merged.get("source"), dict) else {}
    provider = str(source.get("provider") or chosen.source or "metadata_resolution")
    # raw_record_path must always point at a metadata source record, never at
    # fetch_result.json. Use source_records/metadata_source.<provider>.json.
    from src.metadata.source_records import ensure_raw_record_path_is_metadata_source
    raw_record_path = ensure_raw_record_path_is_metadata_source(
        source.get("raw_record_path") or "", provider,
    )
    source["raw_record_path"] = raw_record_path
    merged["source"] = source
    write_metadata_source_record(folder, provider, chosen.to_dict())
    atomic_write_json(meta_path, merged, indent=2)
    write_import_status(folder, "metadata_resolved", reason=f"citation metadata selected from candidate {chosen.candidate_id}; PDF match pending")

    report.applied = True
    report.applied_status = new_status
    report.chosen_candidate_id = chosen.candidate_id

    return {"applied": True, "status": new_status, "paper_number": source_id, "paper_raw_id": source_id,
            "chosen_candidate_id": chosen.candidate_id, "doi": chosen.doi, "warnings": merge_warnings}
