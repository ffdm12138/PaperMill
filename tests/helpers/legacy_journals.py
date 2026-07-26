"""Builders for real-schema legacy pending-page journals (test/fixture use).

Mirrors the strict contract in
``src.migrations.discovery_v4.legacy_contracts.page_journal_v3``: the exact
22 top-level keys, the 14 core candidate wrapper keys, and the 19 inner
candidate keys verified against the 1875 production journals.
"""
from __future__ import annotations

from typing import Any


def make_inner_candidate(
    doi: str = "10.5555/example",
    *,
    title: str = "Example legacy title",
    year: int | None = 2020,
) -> dict[str, Any]:
    """Inner ``candidate`` record with the exact production key set."""
    return {
        "title": title,
        "year": year,
        "authors": ["Au, One", "Au, Two"],
        "doi": doi,
        "venue": "Example Venue",
        "abstract": "Example abstract.",
        "source": "openalex",
        "source_id": f"https://openalex.org/W{abs(hash(doi)) % 10**10}",
        "url": f"https://doi.org/{doi}" if doi else "https://openalex.org/W1",
        "pdf_url": "",
        "open_access": False,
        "citation_count": 0,
        "confidence": 0.0,
        "query": "example query",
        "domain_id": None,
        "doi_resolution": {},
        "existing_duplicate_refs": [],
        "duplicate_indexed": False,
        "raw": {"id": "example"},
    }


def make_relevance(state: str = "passed") -> dict[str, Any]:
    """Relevance verdict with the exact production key set."""
    return {
        "state": state,
        "profile_hash": "0" * 64,
        "matched_groups": {"object": [], "process": []},
        "negative_matches": [],
        "reason": "profile_match" if state == "passed" else "missing_required_group",
        "verification": {},
        "attempt_count": 0,
        "next_retry_at": None,
        "last_attempt_at": "2026-01-01T00:00:00+00:00",
        "last_error_class": None,
        "last_http_status": None,
    }


def make_candidate(
    status: str = "pending",
    doi: str = "10.5555/example",
    *,
    candidate_id: str = "c" + "0" * 31,
    relevance_state: str | None = None,
    staged_paper_number: str | None = None,
    reconciled: bool = False,
    terminal_reason: str | None = None,
) -> dict[str, Any]:
    """Candidate wrapper with the exact production core key set."""
    wrapper: dict[str, Any] = {
        "candidate_id": candidate_id,
        "status": status,
        "attempts": 0,
        "last_error": None,
        "terminal_reason": terminal_reason,
        "staged_paper_number": staged_paper_number,
        "claimed_by": "worker-1" if status == "processing" else None,
        "claimed_at": "2026-01-01T00:00:00+00:00" if status == "processing" else None,
        "lease_expires_at": "2026-01-01T01:00:00+00:00" if status == "processing" else None,
        "export_id": "exp-1" if status == "emitted" else None,
        "export_path": "exports/exp-1.jsonl" if status == "emitted" else None,
        "emitted_at": "2026-01-01T00:00:00+00:00" if status == "emitted" else None,
        "reconciled": reconciled,
        "candidate": make_inner_candidate(doi),
    }
    if relevance_state is not None:
        wrapper["relevance"] = make_relevance(relevance_state)
    return wrapper


def make_statistics(candidates: list[dict[str, Any]]) -> dict[str, int]:
    """Statistics block consistent with the candidate list (full variant)."""
    stats = {
        "returned": len(candidates),
        "pending": 0,
        "terminal": 0,
        "staged": 0,
        "emitted": 0,
        "existing_duplicate": 0,
        "duplicate_observation": 0,
        "invalid": 0,
        "unresolved": 0,
        "failed_retryable": 0,
        "failed_terminal": 0,
        "relevance_profile_unbound": 0,
        "relevance_passed": 0,
        "relevance_rejected": 0,
        "relevance_verification_deferred": 0,
        "relevance_candidate_invalid": 0,
    }
    for cand in candidates:
        status = cand["status"]
        if status in stats:
            stats[status] += 1
        rel = cand.get("relevance")
        if isinstance(rel, dict) and rel.get("state") == "rejected":
            stats["relevance_rejected"] += 1
        elif isinstance(rel, dict) and rel.get("state") == "passed":
            stats["relevance_passed"] += 1
    return stats


def make_journal(
    candidates: list[dict[str, Any]],
    *,
    keyword_id: str = "a" * 16,
    keyword_zh: str = "测试关键词",
    query_id: str = "b" * 16,
    query: str = "example query",
    query_language: str = "en",
    provider: str = "openalex",
    lane: str = "backfill",
    page_id: str = "p" + "0" * 31,
    state: str = "drained",
) -> dict[str, Any]:
    """Legacy pending-page journal with the exact production key set."""
    return {
        "schema_version": "2.0",
        "page_id": page_id,
        "keyword_id": keyword_id,
        "keyword_zh": keyword_zh,
        "query_id": query_id,
        "query": query,
        "query_language": query_language,
        "provider": provider,
        "lane": lane,
        "generation": 2,
        "request_cursor": "*",
        "next_cursor": "cursor-next",
        "request_signature": {"sort": "relevance", "filters": {}},
        "page_sequence": None,
        "refresh_run_id": None,
        "fetched_at": "2026-01-01T00:00:00+00:00",
        "cursor_committed_at": "2026-01-01T00:00:01+00:00",
        "drained_at": "2026-01-01T00:00:02+00:00" if state == "drained" else None,
        "state": state,
        "provider_exhausted": False,
        "statistics": make_statistics(candidates),
        "candidates": candidates,
    }
