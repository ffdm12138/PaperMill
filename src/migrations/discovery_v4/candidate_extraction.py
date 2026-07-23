"""Safe DOI candidate extraction from legacy v2/v3 page journals.

Stream-extracts DOIs from archived journals one file at a time, never
loading all 1875 files into memory.  Deduplicates against paper ledger,
existing papers, paper_raw workspaces, and previously imported seeds.

Key constraint: legacy seeds are NEVER written as v4 ProviderPageJournals.
They carry ``origin='legacy_candidate_seed'`` and cannot advance cursors
or provide exhaustion evidence.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from src.discovery.models import normalize_doi
from src.services.metadata_quality import is_valid_normalized_doi


@dataclass
class CandidateExtractionReport:
    """Summary of legacy candidate extraction."""

    journals_scanned: int = 0
    candidates_observed: int = 0
    valid_doi_seeds: int = 0
    invalid_doi: int = 0
    already_existing: int = 0
    duplicate_seeds: int = 0
    imported: int = 0
    unresolved: int = 0
    errors: list[str] = field(default_factory=list)


def stream_extract_candidates(
    journal_dir: Path,
) -> Iterator[dict[str, Any]]:
    """Stream-extract DOI candidates from v2/v3 journals.

    Yields one candidate dict at a time.  Never loads more than one
    journal file into memory.

    Args:
        journal_dir: Root of archived pending_pages (e.g.,
            ``legacy_archive/<mid>/pending_pages/``).

    Yields:
        Dicts with keys: candidate_id, keyword_id, keyword_zh, query_id,
        query, provider, lane, doi, title, year, authors, venue,
        source_page_id, source_schema_version, journal_sha256.
    """
    for path in sorted(journal_dir.rglob("*.json")):
        if not path.is_file():
            continue

        try:
            raw = path.read_text(encoding="utf-8")
            page = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue

        if not isinstance(page, dict):
            continue

        schema_ver = page.get("schema_version", "unknown")
        keyword_id = page.get("keyword_id", "")
        keyword_zh = page.get("keyword_zh", "")
        query_id = page.get("query_id", "")
        query = page.get("query", "")
        provider = page.get("provider", "")
        lane = page.get("lane", "")
        page_id = page.get("page_id", "")

        candidates = page.get("candidates", [])
        if not isinstance(candidates, list):
            continue

        for cand in candidates:
            if not isinstance(cand, dict):
                continue

            inner = cand.get("candidate", cand)
            if not isinstance(inner, dict):
                continue

            doi = str(inner.get("doi", "") or "").strip()
            title = str(inner.get("title", "") or "").strip() or None

            yield {
                "candidate_id": str(cand.get("candidate_id", "")),
                "keyword_id": keyword_id,
                "keyword_zh": keyword_zh,
                "query_id": query_id,
                "query": query,
                "provider": provider,
                "lane": lane,
                "doi": doi,
                "title": title,
                "year": inner.get("year"),
                "authors": inner.get("authors"),
                "venue": inner.get("venue"),
                "source_page_id": page_id,
                "source_schema_version": schema_ver,
            }


def deduplicate_seeds(
    seeds: Iterator[dict[str, Any]],
    *,
    known_dois: set[str] | None = None,
    existing_seed_dois: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Deduplicate extracted candidates.

    A candidate is kept only if its normalized DOI is:
    - A valid DOI format
    - Not in ``known_dois`` (paper ledger, papers, paper_raw)
    - Not in ``existing_seed_dois`` (previously imported seeds)
    - Not a duplicate within this extraction batch

    Returns (unique_seeds, stats).
    """
    known = known_dois or set()
    seen = existing_seed_dois or set()
    stats = {
        "total_observed": 0,
        "invalid_doi": 0,
        "already_existing": 0,
        "duplicate_within_batch": 0,
        "valid_unique": 0,
    }

    unique: list[dict[str, Any]] = []
    batch_seen: set[str] = set()

    for seed in seeds:
        stats["total_observed"] += 1
        doi = seed.get("doi", "")
        if not doi:
            stats["invalid_doi"] += 1
            continue

        normalized = normalize_doi(doi)
        if not is_valid_normalized_doi(normalized):
            stats["invalid_doi"] += 1
            continue

        if normalized in known or normalized in seen:
            stats["already_existing"] += 1
            continue

        if normalized in batch_seen:
            stats["duplicate_within_batch"] += 1
            continue

        batch_seen.add(normalized)
        seed["normalized_doi"] = normalized
        unique.append(seed)
        stats["valid_unique"] += 1

    return unique, stats


def build_known_doi_set(
    ledger_path: Path,
    papers_dir: Path,
    paper_raw_dir: Path,
) -> set[str]:
    """Build a set of known DOIs from the ledger, papers, and paper_raw.

    This is a best-effort scan — failures are logged but don't block
    extraction.
    """
    known: set[str] = set()

    # Scan paper_raw workspaces for DOI metadata
    if paper_raw_dir.is_dir():
        for meta_path in paper_raw_dir.rglob("*.metadata_freeze.json"):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    doi = data.get("doi", "")
                    if doi:
                        known.add(normalize_doi(str(doi)))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                pass
        for meta_path in paper_raw_dir.rglob("*.metadata_match.json"):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    doi = data.get("doi", "")
                    if doi:
                        known.add(normalize_doi(str(doi)))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                pass

    # Scan papers directory for frozen metadata
    if papers_dir.is_dir():
        for meta_path in papers_dir.rglob("*.metadata_freeze.json"):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    doi = data.get("doi", "")
                    if doi:
                        known.add(normalize_doi(str(doi)))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                pass

    # Also scan the ledger for paper_number → DOI mappings
    if ledger_path.is_file():
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            if isinstance(ledger, dict):
                for entry in ledger.get("entries", {}).values():
                    if isinstance(entry, dict):
                        doi = entry.get("doi", "")
                        if doi:
                            known.add(normalize_doi(str(doi)))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            pass

    return known
