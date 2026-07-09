"""End-to-end DOI discovery pipeline."""
from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from config.settings import DISCOVERY_DIR, DISCOVERY_KEYWORD_NOTEBOOK_DIR
from src.discovery.keyword_notebook import (
    INITIAL_CURSOR,
    PROVIDERS,
    KeywordNotebookStore,
    pagination_signature,
)
from src.discovery.models import CandidateBatch, PaperCandidate
from src.discovery.query_expand import expand_query
from src.discovery.rank_candidates import (
    dedupe_and_rank_candidates,
    merge_and_dedupe_candidates,
    rank_candidates,
)
from src.discovery.resolve_crossref import (
    resolve_doi_by_title,
    search_crossref,
    search_crossref_page,
)
from src.discovery.search_openalex import search_openalex, search_openalex_page
from src.discovery.coordinator import DiscoveryOptions, run_discovery_batch
from src.discovery.page_journal import PageJournalStore
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


# ── Dual-lane (Refresh + Backfill) discovery ─────────────────────────


DiscoveryMode = Literal["refresh", "backfill", "hybrid"]
DiscoveryLane = Literal["refresh", "backfill"]

_UNTIL_EXHAUSTED_HARD_CAP = 200


def _fetch_page(
    provider: str,
    query: str,
    *,
    original_keyword: str,
    lane: DiscoveryLane,
    page_size: int,
    cursor: str,
    sort: str | None,
    domain_id: str | None,
    rate_limiter: Any | None,
    limiter_lock: threading.Lock | None,
):
    if provider == "openalex":
        return search_openalex_page(
            query,
            original_keyword=original_keyword,
            lane=lane,
            page_size=page_size,
            cursor=cursor,
            sort=sort,
            domain_id=domain_id,
            rate_limiter=rate_limiter,
            limiter_lock=limiter_lock,
        )
    if provider == "crossref":
        return search_crossref_page(
            query,
            original_keyword=original_keyword,
            lane=lane,
            page_size=page_size,
            cursor=cursor,
            sort=sort,
            domain_id=domain_id,
            rate_limiter=rate_limiter,
            limiter_lock=limiter_lock,
        )
    raise ValueError(f"unknown provider: {provider}")


def _run_refresh_lane(
    keyword: str,
    expansions: dict[str, dict],
    store: KeywordNotebookStore,
    *,
    refresh_pages: int,
    page_size: int,
    sort: str | None,
    domain_id: str | None,
    rate_limiter: Any | None,
    limiter_lock: threading.Lock | None,
) -> tuple[list[PaperCandidate], int, int]:
    """Scan ``refresh_pages`` from page 1 for every active expansion×provider.

    Refresh never touches the backfill cursor. Updates only refresh state.
    """
    all_candidates: list[PaperCandidate] = []
    pages_scanned = 0
    provider_failures = 0
    for ekey, exp in expansions.items():
        if not exp.get("active"):
            continue
        query = exp.get("query", "")
        for provider in PROVIDERS:
            store.begin_refresh(keyword, ekey, provider)
            cursor = INITIAL_CURSOR
            items = 0
            pages = 0
            errors: list[str] = []
            for _ in range(refresh_pages):
                page = _fetch_page(
                    provider, query,
                    original_keyword=keyword, lane="refresh",
                    page_size=page_size, cursor=cursor, sort=sort,
                    domain_id=domain_id,
                    rate_limiter=rate_limiter, limiter_lock=limiter_lock,
                )
                pages += 1
                pages_scanned += 1
                if page.status == "failed":
                    provider_failures += 1
                    errors.append(page.safe_error or page.error_type or "failed")
                    break
                all_candidates.extend(page.candidates)
                items += page.returned_count
                if page.exhausted or not page.next_cursor:
                    break
                cursor = page.next_cursor
            status = "success" if not errors else ("partial_success" if items > 0 else "failed")
            store.complete_refresh(
                keyword, ekey, provider,
                status=status, pages_scanned=pages,
                items_returned=items,
                error="; ".join(errors) if errors else None,
            )
    return all_candidates, pages_scanned, provider_failures


def _run_backfill_lane(
    keyword: str,
    expansions: dict[str, dict],
    store: KeywordNotebookStore,
    *,
    backfill_pages: int,
    page_size: int,
    sort: str | None,
    domain_id: str | None,
    rate_limiter: Any | None,
    limiter_lock: threading.Lock | None,
    until_exhausted: bool = False,
    max_pages_total: int | None = None,
) -> tuple[list[PaperCandidate], int, int, int]:
    """Continue each non-exhausted expansion×provider from its saved cursor.

    Within one provider/expansion the cursor chain is sequential. On
    failure the cursor is NOT advanced and the lane stops that chain.
    """
    all_candidates: list[PaperCandidate] = []
    pages_advanced = 0
    provider_failures = 0
    exhausted_states = 0
    hard_cap = (
        min(max_pages_total or _UNTIL_EXHAUSTED_HARD_CAP, _UNTIL_EXHAUSTED_HARD_CAP)
        if until_exhausted
        else backfill_pages
    )
    for ekey, exp in expansions.items():
        if not exp.get("active"):
            continue
        query = exp.get("query", "")
        for provider in PROVIDERS:
            if store.is_backfill_exhausted(keyword, ekey, provider):
                exhausted_states += 1
                continue
            cursor = store.get_backfill_cursor(keyword, ekey, provider)
            items = 0
            pages = 0
            had_error = False
            while pages < hard_cap:
                page = _fetch_page(
                    provider, query,
                    original_keyword=keyword, lane="backfill",
                    page_size=page_size, cursor=cursor, sort=sort,
                    domain_id=domain_id,
                    rate_limiter=rate_limiter, limiter_lock=limiter_lock,
                )
                if page.status == "failed":
                    provider_failures += 1
                    had_error = True
                    store.record_backfill_error(
                        keyword, ekey, provider,
                        error=page.safe_error or page.error_type or "failed",
                    )
                    break
                all_candidates.extend(page.candidates)
                items += page.returned_count
                pages += 1
                pages_advanced += 1
                store.advance_backfill(
                    keyword, ekey, provider,
                    next_cursor=page.next_cursor,
                    items_this_page=page.returned_count,
                    exhausted=page.exhausted,
                )
                if page.exhausted or not page.next_cursor:
                    exhausted_states += 1
                    break
                cursor = page.next_cursor
    return all_candidates, pages_advanced, provider_failures, exhausted_states


def _lane_status(ran: bool, failures: int, items: int) -> str:
    if not ran:
        return "skipped"
    if failures == 0:
        return "success"
    if items > 0:
        return "partial_success"
    return "failed"


def discover_papers_dual_lane(
    keyword: str,
    *,
    mode: DiscoveryMode = "hybrid",
    refresh_pages: int = 2,
    backfill_pages: int = 5,
    page_size: int = 50,
    domain_id: str | None = None,
    max_candidates: int = 50,
    output_dir: Path | None = None,
    paper_raw_dir: Path | None = None,
    papers_dir: Path | None = None,
    hide_existing: bool = False,
    notebook_dir: Path | None = None,
    rate_limiter: Any | None = None,
    sort: str | None = None,
    until_exhausted: bool = False,
    max_pages_total: int | None = None,
    doi_resolution_budget: int = 10,
) -> tuple[CandidateBatch, dict]:
    """Compatibility wrapper around the transactional coordinator."""
    nb_dir = Path(notebook_dir) if notebook_dir else DISCOVERY_KEYWORD_NOTEBOOK_DIR
    runtime_base = nb_dir / ".discovery_runtime" if notebook_dir else DISCOVERY_DIR

    def _fetch(provider: str, query: str, **kwargs: Any) -> Any:
        if provider == "openalex":
            return search_openalex_page(query, **kwargs)
        if provider == "crossref":
            return search_crossref_page(query, **kwargs)
        raise ValueError(f"unknown provider: {provider}")

    options = DiscoveryOptions(
        mode=mode,
        refresh_pages=refresh_pages,
        backfill_pages=backfill_pages,
        page_size=page_size,
        domain_id=domain_id,
        max_candidates=max_candidates,
        stage_to_paper_raw=False,
        apply=False,
        hide_existing=hide_existing,
        until_exhausted=until_exhausted,
        max_pages_total=max_pages_total,
        doi_resolution_budget=doi_resolution_budget,
        openalex_refresh_sort=sort,
        openalex_backfill_sort=sort,
        crossref_refresh_sort=sort,
        crossref_backfill_sort=sort,
        notebook_dir=nb_dir,
        pending_pages_dir=runtime_base / "pending_pages",
        locks_dir=runtime_base / "locks",
        exports_dir=runtime_base / "exports",
        output_dir=Path(output_dir) if output_dir else DISCOVERY_DIR / "doi_candidates",
        paper_raw_dir=paper_raw_dir or (runtime_base / "paper_raw"),
        papers_dir=papers_dir or (runtime_base / "papers"),
    )
    batch_report = run_discovery_batch([keyword], options=options, max_workers=2, fetch_page=_fetch)
    report_obj = batch_report.keywords[0]
    journal = PageJournalStore(options.pending_pages_dir)
    candidates: list[PaperCandidate] = []
    for ref in journal.list_pages([report_obj.keyword_id]):
        data = journal.read(ref.path)
        for item in data.get("candidates", []):
            if item.get("status") in {"emitted", "staged"} and isinstance(item.get("candidate"), dict):
                candidates.append(PaperCandidate.from_dict(item["candidate"]))
    batch = CandidateBatch(
        original_query=keyword,
        expanded_queries=[],
        candidates=candidates[:max_candidates],
        sources=["openalex", "crossref"],
    )
    report = report_obj.to_dict()
    report["batch_status"] = batch_report.status
    return batch, report


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
