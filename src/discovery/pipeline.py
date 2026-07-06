"""End-to-end DOI discovery pipeline."""
import json
import re
from datetime import datetime
from pathlib import Path

from loguru import logger

from config.settings import DISCOVERY_DIR
from src.discovery.models import CandidateBatch, PaperCandidate
from src.discovery.query_expand import expand_query
from src.discovery.rank_candidates import dedupe_and_rank_candidates
from src.discovery.resolve_crossref import resolve_doi_by_title, search_crossref
from src.discovery.search_openalex import search_openalex
from src.services.ingest_duplicate_guard import check_doi_duplicate


def slugify_query(query: str, max_len: int = 48) -> str:
    slug = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "_", query).strip("_")
    return (slug or "query")[:max_len]


def _write_batch(
    batch: CandidateBatch,
    output_dir: Path,
    query_slug: str,
    *,
    summary_extra: dict | None = None,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    jsonl_path = output_dir / f"{stamp}_{query_slug}.jsonl"
    summary_path = output_dir / f"{stamp}_{query_slug}_summary.json"
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for candidate in batch.candidates:
            fh.write(json.dumps(candidate.to_dict(), ensure_ascii=False) + "\n")
    summary = batch.to_dict()
    summary["candidate_count"] = len(batch.candidates)
    summary["jsonl_path"] = jsonl_path.as_posix()
    if summary_extra:
        summary.update(summary_extra)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonl_path, summary_path


def _fill_missing_dois(candidates: list[PaperCandidate], limit: int = 10) -> None:
    checked = 0
    for candidate in candidates:
        if candidate.doi or not candidate.title:
            continue
        match = resolve_doi_by_title(candidate.title, year=candidate.year, domain_id=candidate.domain_id)
        checked += 1
        if match and match.doi:
            candidate.doi = match.doi
            candidate.raw.setdefault("crossref_resolution", match.to_dict())
            candidate.source = ",".join(sorted(set([candidate.source, "crossref"]) - {""}))
        if checked >= limit:
            break


def _annotate_existing_duplicates(
    candidates: list[PaperCandidate],
    *,
    paper_raw_dir: Path,
    papers_dir: Path,
) -> int:
    detected = 0
    for candidate in candidates:
        doi = candidate.doi
        if not doi:
            continue
        dup = check_doi_duplicate(doi, paper_raw_dir=paper_raw_dir, papers_dir=papers_dir)
        candidate.duplicate_indexed = True
        candidate.existing_duplicate_refs = [ref.to_dict() for ref in dup.refs]
        if dup.refs:
            detected += 1
    return detected


def discover_papers(
    query: str,
    domain_id: str | None = None,
    limit_per_query: int = 15,
    max_candidates: int = 50,
    output_dir: Path | None = None,
    paper_raw_dir: Path | None = None,
    papers_dir: Path | None = None,
    hide_existing: bool = False,
) -> CandidateBatch:
    expanded = expand_query(query, domain_id=domain_id)
    candidates: list[PaperCandidate] = []
    for expanded_query in expanded["expanded_queries"]:
        candidates.extend(search_openalex(expanded_query, domain_id=domain_id, limit=limit_per_query))
        candidates.extend(search_crossref(expanded_query, domain_id=domain_id, limit=limit_per_query))

    _fill_missing_dois(candidates)
    ranked = dedupe_and_rank_candidates(candidates, query=query, max_candidates=max_candidates)
    total_candidates_before_filter = len(ranked)
    existing_duplicates_detected = 0
    if paper_raw_dir is not None and papers_dir is not None:
        existing_duplicates_detected = _annotate_existing_duplicates(
            ranked,
            paper_raw_dir=paper_raw_dir,
            papers_dir=papers_dir,
        )
    visible = [
        candidate
        for candidate in ranked
        if not (hide_existing and candidate.existing_duplicate_refs)
    ]
    hidden_existing_duplicates = total_candidates_before_filter - len(visible)
    batch = CandidateBatch(
        original_query=query,
        expanded_queries=expanded["expanded_queries"],
        candidates=visible,
        sources=["openalex", "crossref"],
    )

    destination = output_dir or (DISCOVERY_DIR / "doi_candidates")
    jsonl_path, summary_path = _write_batch(
        batch,
        Path(destination),
        slugify_query(query),
        summary_extra={
            "total_candidates_before_filter": total_candidates_before_filter,
            "existing_duplicates_detected": existing_duplicates_detected,
            "hidden_existing_duplicates": hidden_existing_duplicates,
            "visible_candidates": len(visible),
        },
    )
    logger.info(f"Wrote DOI candidates to {jsonl_path}")
    logger.info(f"Wrote DOI summary to {summary_path}")
    return batch
