"""Durable provider-page journal for DOI discovery.

The journal is the safety boundary between provider pagination and candidate
processing. Backfill cursors may advance only after a provider page is persisted
here. Candidate state also lives here so ``max_candidates`` can stop a run
without losing unprocessed observations.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping

from filelock import FileLock

from src.discovery.keyword_notebook import (
    PROVIDERS,
    detect_query_language,
    keyword_id as make_keyword_id,
    normalize_keyword,
    query_identity,
)
from src.discovery.models import PaperCandidate, normalize_doi, normalize_title
from src.discovery.relevance import (
    RELEVANCE_REASON_VALUES,
    RELEVANCE_STATES,
    RelevanceReason,
)
from src.utils.atomic_io import atomic_replace_bytes_unlocked


from src.discovery.constants import INITIAL_CURSOR


PAGE_SCHEMA_VERSION = "2.0"

# ── Exact field set for v2 page journals ───────────────────────────
# ALL_V2_FIELDS is used to reject unknown fields.
# REQUIRED_V2_FIELDS (a subset) is used to reject missing critical fields.
PAGE_V2_FIELDS: frozenset[str] = frozenset({
    "schema_version", "page_id", "keyword_id", "keyword_zh",
    "query_id", "query", "query_language", "provider", "lane",
    "generation", "request_signature",
    "request_cursor", "next_cursor",
    "provider_exhausted", "state",
    "fetched_at", "cursor_committed_at", "drained_at",
    "candidates", "statistics",
    "refresh_run_id", "page_sequence",
})

# Backwards-compatible aliases used by the v3 migration module.
PAGE_ALL_V2_FIELDS = PAGE_V2_FIELDS
PAGE_REQUIRED_V2_FIELDS = PAGE_V2_FIELDS

PageLane = Literal["refresh", "backfill"]
PageState = Literal["fetched", "cursor_committed", "draining", "drained", "failed"]
CandidateState = Literal[
    "pending",
    "resolution_pending",
    "ready",
    "processing",
    "staged",
    "emitted",
    "existing_duplicate",
    "duplicate_observation",
    "invalid_doi",
    "unresolved",
    "failed_retryable",
    "failed_terminal",
]

RELEVANCE_TERMINAL_STATES = {"rejected", "candidate_invalid"}
RELEVANCE_CLAIMABLE_STATES = {"passed"}
RELEVANCE_PROFILE_CHANGE_CLOSEABLE_STATES = {
    "profile_unbound", "passed", "verification_deferred",
}

TERMINAL_CANDIDATE_STATES = {
    "staged",
    "emitted",
    "existing_duplicate",
    "duplicate_observation",
    "invalid_doi",
    "unresolved",
    "failed_terminal",
}
NONTERMINAL_CANDIDATE_STATES = {
    "pending",
    "resolution_pending",
    "ready",
    "processing",
    "failed_retryable",
}

PROFILE_CLOSEABLE_CANDIDATE_STATES = frozenset({
    "pending", "resolution_pending", "ready",
})
PROFILE_RECOVERY_REQUIRED_CANDIDATE_STATES = frozenset({
    "processing", "failed_retryable",
})
DURABLE_DOI_CANDIDATE_STATES = frozenset({
    "staged", "emitted", "existing_duplicate", "duplicate_observation",
})


class CandidateLifecycleClass(str, Enum):
    PRE_STAGING_CLOSEABLE = "pre_staging_closeable"
    RECOVERY_REQUIRED = "recovery_required"
    COMPLETED_TERMINAL = "completed_terminal"
    INVALID = "invalid"


def classify_candidate_lifecycle(status: Any) -> CandidateLifecycleClass:
    """Classify lifecycle facts once for profile transactions and indexes."""
    value = str(status or "")
    if value in PROFILE_CLOSEABLE_CANDIDATE_STATES:
        return CandidateLifecycleClass.PRE_STAGING_CLOSEABLE
    if value in PROFILE_RECOVERY_REQUIRED_CANDIDATE_STATES:
        return CandidateLifecycleClass.RECOVERY_REQUIRED
    if value in TERMINAL_CANDIDATE_STATES:
        return CandidateLifecycleClass.COMPLETED_TERMINAL
    return CandidateLifecycleClass.INVALID


def is_profile_closeable_candidate(
    candidate: Mapping[str, Any], *, target_profile_hash: str,
) -> bool:
    relevance = candidate.get("relevance")
    old_hash = (
        str(relevance.get("profile_hash") or "")
        if isinstance(relevance, Mapping) else ""
    )
    return bool(
        classify_candidate_lifecycle(candidate.get("status"))
        is CandidateLifecycleClass.PRE_STAGING_CLOSEABLE
        and _relevance_state(candidate) in RELEVANCE_PROFILE_CHANGE_CLOSEABLE_STATES
        and old_hash != target_profile_hash
    )

_PAGE_TRANSITIONS = {
    "fetched": {"cursor_committed", "failed"},
    "cursor_committed": {"draining", "drained"},
    "draining": {"cursor_committed", "drained"},
    "drained": set(),
    "failed": set(),
}

_CANDIDATE_TRANSITIONS = {
    "pending": {
        "resolution_pending",
        "ready",
        "processing",
        "existing_duplicate",
        "duplicate_observation",
        "invalid_doi",
        "unresolved",
        "failed_terminal",
    },
    "resolution_pending": {
        "ready",
        "processing",
        "duplicate_observation",
        "unresolved",
        "failed_retryable",
        "failed_terminal",
    },
    "ready": {
        "processing",
        "existing_duplicate",
        "duplicate_observation",
        "invalid_doi",
        "failed_terminal",
    },
    "processing": {
        "staged",
        "emitted",
        "existing_duplicate",
        "duplicate_observation",
        "invalid_doi",
        "unresolved",
        "failed_retryable",
        "failed_terminal",
    },
    "failed_retryable": {"processing"},
    "staged": set(),
    "emitted": set(),
    "existing_duplicate": set(),
    "duplicate_observation": set(),
    "invalid_doi": set(),
    "unresolved": set(),
    "failed_terminal": set(),
}


class JournalCorruptError(RuntimeError):
    """Raised when a page journal is missing required structure or bad JSON."""


class InvalidStateTransition(RuntimeError):
    """Raised when code attempts a forbidden page/candidate transition."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def stable_hash(*parts: Any, length: int = 32) -> str:
    payload = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def request_signature(
    *,
    sort: str | None = None,
    filters: dict[str, Any] | None = None,
    page_size: int,
    pagination_schema_version: str = "2.0",
) -> dict[str, Any]:
    return {
        "sort": sort or "",
        "filters": filters or {},
        "page_size": int(page_size),
        "pagination_schema_version": pagination_schema_version,
        "hash": stable_hash(
            sort or "",
            json.dumps(filters or {}, ensure_ascii=False, sort_keys=True),
            int(page_size),
            pagination_schema_version,
            length=16,
        ),
    }


def backfill_page_id(
    *,
    keyword_id: str,
    query_id: str,
    provider: str,
    request_signature_hash: str,
    request_cursor: str | None,
) -> str:
    return stable_hash(
        "backfill",
        keyword_id,
        query_id,
        provider,
        request_signature_hash,
        request_cursor or INITIAL_CURSOR,
    )


def refresh_page_id(
    *,
    keyword_id: str,
    query_id: str,
    provider: str,
    request_signature_hash: str,
    refresh_run_id: str,
    page_sequence: int,
) -> str:
    return stable_hash(
        "refresh",
        keyword_id,
        query_id,
        provider,
        request_signature_hash,
        refresh_run_id,
        int(page_sequence),
    )


def provider_record_id(candidate: PaperCandidate) -> str:
    raw = candidate.raw if isinstance(candidate.raw, dict) else {}
    for key in ("id", "openalex_id", "crossref_id", "DOI", "doi"):
        value = raw.get(key)
        if value:
            return str(value)
    return str(candidate.source_id or "")


def candidate_id(page_id: str, candidate: PaperCandidate, page_item_index: int) -> str:
    rid = provider_record_id(candidate)
    if rid:
        return stable_hash(page_id, rid)
    doi = normalize_doi(candidate.doi)
    if doi:
        return stable_hash(page_id, doi)
    return stable_hash(page_id, normalize_title(candidate.title), int(page_item_index))


def title_resolution_key(candidate: dict[str, Any] | PaperCandidate) -> str:
    if isinstance(candidate, PaperCandidate):
        title = candidate.title
        year = candidate.year
        authors = candidate.authors
    else:
        payload = candidate.get("candidate") if isinstance(candidate.get("candidate"), dict) else candidate
        title = str(payload.get("title") or "")
        year = payload.get("year")
        authors = payload.get("authors") or []
    first_author = ""
    if isinstance(authors, list) and authors:
        first = authors[0]
        first_author = str(first.get("full_name") or first.get("name") if isinstance(first, dict) else first)
    return stable_hash("resolution", normalize_title(title), year or "", first_author.lower(), length=40)


def _candidate_record(
    page_id_value: str,
    candidate: PaperCandidate,
    index: int,
    *,
    relevance_profile_hash: str | None = None,
) -> dict[str, Any]:
    cid = candidate_id(page_id_value, candidate, index)
    payload = candidate.to_dict()
    record = {
        "candidate_id": cid,
        "candidate": payload,
        "status": "pending",
        "attempts": 0,
        "last_error": None,
        "terminal_reason": None,
        "staged_paper_number": None,
        "claimed_by": None,
        "claimed_at": None,
        "lease_expires_at": None,
        "export_id": None,
        "export_path": None,
        "emitted_at": None,
        "reconciled": False,
    }
    if relevance_profile_hash is not None:
        record["relevance"] = {
            "state": "profile_unbound",
            "profile_hash": relevance_profile_hash,
            "matched_groups": {},
            "negative_matches": [],
            "reason": "",
            "verification": {},
            "attempt_count": 0,
            "next_retry_at": None,
            "last_attempt_at": None,
            "last_error_class": None,
            "last_http_status": None,
        }
    return record


def _atomic_write_json_unlocked(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically (caller holds lock) with fsync durability.

    Delegates to :func:`src.utils.atomic_io.atomic_write_json_unlocked`
    so that all durable writers share the same fsync + tmp + os.replace
    + parent-dir-fsync implementation.
    """
    from src.utils.atomic_io import atomic_write_json_unlocked as _unlocked

    _unlocked(path, data, indent=2)


def validate_page(data: Any, path: Path | None = None) -> dict[str, Any]:
    """Strictly validate one active schema-v2 provider-page journal."""
    if not isinstance(data, dict):
        raise JournalCorruptError(f"journal root is not object: {path or ''}")
    missing = sorted(PAGE_V2_FIELDS - set(data))
    if missing:
        raise JournalCorruptError(f"journal missing keys {missing}: {path or ''}")
    unexpected = sorted(set(data) - PAGE_V2_FIELDS)
    if unexpected:
        raise JournalCorruptError(f"journal contains unexpected fields {unexpected}: {path or ''}")
    if data.get("schema_version") != PAGE_SCHEMA_VERSION:
        raise JournalCorruptError(f"journal schema_version must be {PAGE_SCHEMA_VERSION}: {path or ''}")
    if not isinstance(data.get("page_id"), str) or not data["page_id"]:
        raise JournalCorruptError(f"journal page_id must be non-blank: {path or ''}")
    keyword_zh = data.get("keyword_zh")
    if (
        not isinstance(keyword_zh, str)
        or not keyword_zh.strip()
        or detect_query_language(keyword_zh) not in {"zh", "mixed"}
    ):
        raise JournalCorruptError(f"journal keyword_zh must be non-blank: {path or ''}")
    if data.get("keyword_id") != make_keyword_id(keyword_zh):
        raise JournalCorruptError(f"journal keyword_id does not match keyword_zh: {path or ''}")
    query = data.get("query")
    language = data.get("query_language")
    if not isinstance(query, str) or not query.strip():
        raise JournalCorruptError(f"journal query must be non-blank: {path or ''}")
    if language not in {"zh", "en"} or detect_query_language(query) != language:
        raise JournalCorruptError(f"journal query_language is invalid: {path or ''}")
    expected_query_id = query_identity(language, normalize_keyword(query))
    if data.get("query_id") != expected_query_id:
        raise JournalCorruptError(f"journal query_id does not match query: {path or ''}")
    if data.get("provider") not in PROVIDERS:
        raise JournalCorruptError(f"invalid provider: {data.get('provider')}: {path or ''}")
    if data.get("lane") not in {"refresh", "backfill"}:
        raise JournalCorruptError(f"invalid lane: {data.get('lane')}: {path or ''}")
    generation = data.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise JournalCorruptError(f"journal generation must be a positive integer: {path or ''}")
    if data.get("state") not in _PAGE_TRANSITIONS:
        raise JournalCorruptError(f"invalid page state: {data.get('state')}: {path or ''}")
    signature = data.get("request_signature")
    if not isinstance(signature, dict):
        raise JournalCorruptError(f"journal request_signature must be object: {path or ''}")
    signature_required = {
        "sort", "filters", "page_size", "pagination_schema_version", "hash",
    }
    if not signature_required.issubset(signature):
        raise JournalCorruptError(f"journal request_signature is incomplete: {path or ''}")
    if not isinstance(signature.get("filters"), dict):
        raise JournalCorruptError(f"journal request_signature.filters must be object: {path or ''}")
    try:
        expected_signature = request_signature(
            sort=str(signature.get("sort") or ""),
            filters=signature.get("filters"),
            page_size=int(signature["page_size"]),
            pagination_schema_version=str(signature["pagination_schema_version"]),
        )
    except (TypeError, ValueError) as exc:
        raise JournalCorruptError(f"journal request_signature is invalid: {path or ''}") from exc
    if signature != expected_signature:
        raise JournalCorruptError(f"journal request_signature hash/content mismatch: {path or ''}")
    if data.get("request_cursor") is not None and not isinstance(data.get("request_cursor"), str):
        raise JournalCorruptError(f"journal request_cursor must be string or null: {path or ''}")
    if data.get("next_cursor") is not None and not isinstance(data.get("next_cursor"), str):
        raise JournalCorruptError(f"journal next_cursor must be string or null: {path or ''}")
    if not isinstance(data.get("provider_exhausted"), bool):
        raise JournalCorruptError(f"journal provider_exhausted must be boolean: {path or ''}")
    for field_name in ("fetched_at", "cursor_committed_at", "drained_at"):
        value = data.get(field_name)
        if value is not None and not isinstance(value, str):
            raise JournalCorruptError(f"journal {field_name} must be string or null: {path or ''}")
    if not isinstance(data.get("candidates"), list):
        raise JournalCorruptError(f"journal candidates must be list: {path or ''}")
    for item in data["candidates"]:
        if not isinstance(item, dict) or not isinstance(item.get("candidate_id"), str) or not item.get("candidate_id"):
            raise JournalCorruptError(f"invalid candidate record: {path or ''}")
        status = item.get("status")
        if status not in _CANDIDATE_TRANSITIONS:
            raise JournalCorruptError(f"invalid candidate state {status}: {path or ''}")
        relevance = item.get("relevance")
        if relevance is not None:
            if not isinstance(relevance, dict) or relevance.get("state") not in RELEVANCE_STATES:
                raise JournalCorruptError(f"invalid candidate relevance state: {path or ''}")
            required_relevance = {
                "state", "profile_hash", "matched_groups", "negative_matches",
                "reason", "verification", "attempt_count", "next_retry_at",
                "last_attempt_at", "last_error_class", "last_http_status",
            }
            if not required_relevance.issubset(relevance):
                raise JournalCorruptError(f"candidate relevance is incomplete: {path or ''}")
            if not isinstance(relevance.get("profile_hash"), str):
                raise JournalCorruptError(f"candidate relevance profile_hash is invalid: {path or ''}")
            reason = relevance.get("reason")
            if not isinstance(reason, str) or (reason and reason not in RELEVANCE_REASON_VALUES):
                raise JournalCorruptError(
                    f"candidate relevance reason is unknown_reason: {path or ''}"
                )
            if not isinstance(relevance.get("matched_groups"), dict) or not isinstance(relevance.get("negative_matches"), list):
                raise JournalCorruptError(f"candidate relevance evidence is invalid: {path or ''}")
            if isinstance(relevance.get("attempt_count"), bool) or not isinstance(relevance.get("attempt_count"), int) or relevance.get("attempt_count") < 0:
                raise JournalCorruptError(f"candidate relevance attempt_count is invalid: {path or ''}")
    if not isinstance(data.get("statistics"), dict):
        raise JournalCorruptError(f"journal statistics must be object: {path or ''}")
    return data


_validate_page = validate_page


@dataclass(frozen=True)
class PageRef:
    path: Path
    page_id: str
    keyword_id: str
    query_id: str
    provider: str
    lane: str
    state: str
    fetched_at: str


@dataclass(frozen=True)
class ClaimResult:
    claimed: bool
    page_path: Path
    candidate_id: str
    candidate: dict[str, Any] | None = None
    reason: str = ""


@dataclass(frozen=True)
class CandidateClaim:
    candidate_id: str
    keyword_id: str
    page_id: str
    provider: str
    doi: str
    page_path: Path
    payload: Mapping[str, Any]
    lease_expires_at: str


@dataclass(frozen=True)
class CandidateRef:
    candidate_id: str
    keyword_id: str
    page_path: Path
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class EmittedPrimaryRef:
    candidate_id: str
    page_path: Path
    payload: Mapping[str, Any]


@dataclass
class JournalDrainIndex:
    """One full journal read per batch followed by in-memory lookups."""
    candidate_by_id: dict[str, CandidateRef]
    claimable_by_keyword: dict[str, deque[str]]
    processing_by_doi: dict[str, str]
    emitted_by_doi: dict[str, EmittedPrimaryRef]
    terminal_by_doi: dict[str, list[str]]
    page_cache: dict[Path, dict[str, Any]]
    dirty_pages: set[Path]
    emitted_validation_cache: dict[
        tuple[str, int, int, str, int, int], tuple[bool, str]
    ] = field(default_factory=dict)
    full_scans: int = 1
    pages_read: int = 0
    lookups: int = 0
    delayed_candidate_ids: set[str] = field(default_factory=set)
    active_profile_hashes: dict[str, str] = field(default_factory=dict)
    relevance_updates: int = 0
    binding_invariant_failures: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @classmethod
    def build(
        cls,
        store: "PageJournalStore",
        *,
        active_profile_hashes: Mapping[str, str],
    ) -> "JournalDrainIndex":
        bindings = {str(key): str(value) for key, value in active_profile_hashes.items()}
        if any(not key or not value for key, value in bindings.items()):
            raise ValueError("active relevance profile bindings must be non-blank")
        index = cls(
            {}, {}, {}, {}, {}, {}, set(), pages_read=0,
            active_profile_hashes=bindings,
        )
        if not store.root_dir.exists():
            return index
        now_dt = datetime.now(timezone.utc)
        for path in sorted(store.root_dir.glob("*/*/*/*/*.json")):
            page = store.read(path)
            index.pages_read += 1
            index.page_cache[path] = page
            keyword_id = str(page["keyword_id"])
            expected_profile_hash = bindings.get(keyword_id)
            queue = index.claimable_by_keyword.setdefault(keyword_id, deque())
            for item in page["candidates"]:
                cid = str(item.get("candidate_id") or "")
                if not cid:
                    continue
                index.candidate_by_id[cid] = CandidateRef(cid, keyword_id, path, dict(item))
                doi = _candidate_doi(item)
                status = str(item.get("status") or "")
                expires = parse_iso(item.get("lease_expires_at"))
                next_attempt = parse_iso(item.get("next_attempt_at"))
                claimable = (not next_attempt or next_attempt <= now_dt) and (
                    status in {"pending", "ready", "failed_retryable"}
                    or status == "processing" and (not expires or expires <= now_dt))
                claimable = bool(expected_profile_hash) and claimable and _relevance_claimable(
                    item, expected_profile_hash,
                )
                if claimable and page["state"] in {"cursor_committed", "draining"}:
                    queue.append(cid)
                elif (next_attempt and next_attempt > now_dt
                      and status in {"pending", "ready", "failed_retryable"}
                      and bool(expected_profile_hash)
                      and _relevance_claimable(item, expected_profile_hash)
                      and page["state"] in {"cursor_committed", "draining"}):
                    index.delayed_candidate_ids.add(cid)
                if (
                    status == "processing" and doi and expires and expires > now_dt
                    and bool(expected_profile_hash)
                    and _relevance_claimable(item, expected_profile_hash)
                ):
                    index.processing_by_doi.setdefault(doi, cid)
                elif status == "emitted" and doi:
                    index.emitted_by_doi.setdefault(doi, EmittedPrimaryRef(cid, path, dict(item)))
                if status in DURABLE_DOI_CANDIDATE_STATES and doi:
                    index.terminal_by_doi.setdefault(doi, []).append(cid)
        return index

    def claimable(self, keyword_ids: Iterable[str] | None = None) -> list[CandidateRef]:
        with self._lock:
            self._promote_due_candidates()
            self.lookups += 1
            wanted = set(keyword_ids or self.claimable_by_keyword)
            return [
                self.candidate_by_id[cid]
                for keyword in sorted(wanted)
                for cid in self.claimable_by_keyword.get(keyword, ())
                if _relevance_claimable(
                    self.candidate_by_id[cid].payload,
                    self.active_profile_hashes.get(keyword),
                )
            ]

    def bind_active_profile(self, keyword_id: str, profile_hash_value: str) -> None:
        """Defensively remove stale candidates from transient projections only."""
        with self._lock:
            self.active_profile_hashes[keyword_id] = profile_hash_value
            stale_ids = {
                cid for cid, ref in self.candidate_by_id.items()
                if ref.keyword_id == keyword_id
                and not _relevance_claimable(ref.payload, profile_hash_value)
            }
            queue = self.claimable_by_keyword.setdefault(keyword_id, deque())
            self.claimable_by_keyword[keyword_id] = deque(
                cid for cid in queue if cid not in stale_ids
            )
            self.delayed_candidate_ids.difference_update(stale_ids)
            for doi, cid in list(self.processing_by_doi.items()):
                if cid in stale_ids:
                    self.processing_by_doi.pop(doi, None)
            # Emitted and other durable DOI facts are lifecycle projections;
            # active relevance changes must never remove them.

    def add_page(self, path: Path, page: Mapping[str, Any]) -> None:
        """Publish a freshly persisted page without another full journal scan."""
        with self._lock:
            materialized = dict(page)
            self.page_cache[path] = materialized
            keyword_id = str(page["keyword_id"])
            expected_profile_hash = self.active_profile_hashes.get(keyword_id)
            queue = self.claimable_by_keyword.setdefault(keyword_id, deque())
            now_dt = datetime.now(timezone.utc)
            for raw in page.get("candidates", []):
                item = dict(raw)
                cid = str(item.get("candidate_id") or "")
                if not cid:
                    continue
                self.candidate_by_id[cid] = CandidateRef(cid, keyword_id, path, item)
                status = str(item.get("status") or "")
                expires = parse_iso(item.get("lease_expires_at"))
                next_attempt = parse_iso(item.get("next_attempt_at"))
                if (
                    (not next_attempt or next_attempt <= now_dt)
                    and (
                        status in {"pending", "ready", "failed_retryable"}
                        or status == "processing" and (not expires or expires <= now_dt)
                    )
                    and bool(expected_profile_hash)
                    and _relevance_claimable(item, expected_profile_hash)
                ):
                    queue.append(cid)
                elif next_attempt and next_attempt > now_dt and status in {
                    "pending", "ready", "failed_retryable"
                } and bool(expected_profile_hash) and _relevance_claimable(
                    item, expected_profile_hash,
                ):
                    self.delayed_candidate_ids.add(cid)
                doi = _candidate_doi(item)
                if status == "emitted" and doi:
                    self.emitted_by_doi.setdefault(
                        doi, EmittedPrimaryRef(cid, path, dict(item)))
                if status in DURABLE_DOI_CANDIDATE_STATES and doi:
                    self.terminal_by_doi.setdefault(doi, []).append(cid)

    def pending_count(self, keyword_ids: Iterable[str] | None = None) -> int:
        with self._lock:
            self._promote_due_candidates()
            self.lookups += 1
            wanted = set(keyword_ids or self.claimable_by_keyword)
            return sum(
                1
                for keyword in wanted
                for cid in self.claimable_by_keyword.get(keyword, ())
                if _relevance_claimable(
                    self.candidate_by_id[cid].payload,
                    self.active_profile_hashes.get(keyword),
                )
            )

    def update_candidate(self, page_path: Path, item: Mapping[str, Any]) -> None:
        with self._lock:
            cid = str(item.get("candidate_id") or "")
            old = self.candidate_by_id.get(cid)
            keyword_id = old.keyword_id if old else str(
                (self.page_cache.get(page_path) or {}).get("keyword_id") or "")
            materialized = dict(item)
            self.candidate_by_id[cid] = CandidateRef(
                cid, keyword_id, page_path, materialized)
            self.dirty_pages.add(page_path)
            self.delayed_candidate_ids.discard(cid)
            for queue in self.claimable_by_keyword.values():
                try:
                    queue.remove(cid)
                except ValueError:
                    pass
            for doi, owner in list(self.processing_by_doi.items()):
                if owner == cid:
                    self.processing_by_doi.pop(doi, None)
            for doi, ref in list(self.emitted_by_doi.items()):
                if ref.candidate_id == cid:
                    self.emitted_by_doi.pop(doi, None)
            for doi, owners in list(self.terminal_by_doi.items()):
                if cid in owners:
                    self.terminal_by_doi[doi] = [owner for owner in owners if owner != cid]
                    if not self.terminal_by_doi[doi]:
                        self.terminal_by_doi.pop(doi, None)
            doi = _candidate_doi(materialized)
            status = str(item.get("status") or "")
            expires = parse_iso(item.get("lease_expires_at"))
            next_attempt = parse_iso(item.get("next_attempt_at"))
            now_dt = datetime.now(timezone.utc)
            page_state = str((self.page_cache.get(page_path) or {}).get("state") or "")
            expected_profile_hash = self.active_profile_hashes.get(keyword_id)
            if (
                status == "processing" and doi and expires and expires > now_dt
                and bool(expected_profile_hash)
                and _relevance_claimable(materialized, expected_profile_hash)
            ):
                self.processing_by_doi.setdefault(doi, cid)
            elif status == "emitted" and doi:
                self.emitted_by_doi[doi] = EmittedPrimaryRef(cid, page_path, materialized)
            if status in DURABLE_DOI_CANDIDATE_STATES and doi:
                self.terminal_by_doi.setdefault(doi, []).append(cid)
            if page_state in {"cursor_committed", "draining"} and (
                status in {"pending", "ready", "failed_retryable"}
                or status == "processing" and (not expires or expires <= now_dt)
            ) and bool(expected_profile_hash) and _relevance_claimable(
                materialized, expected_profile_hash,
            ):
                if not next_attempt or next_attempt <= now_dt:
                    self.claimable_by_keyword.setdefault(keyword_id, deque()).append(cid)
                else:
                    self.delayed_candidate_ids.add(cid)

    def _promote_due_candidates(self) -> None:
        now_dt = datetime.now(timezone.utc)
        for cid in tuple(self.delayed_candidate_ids):
            ref = self.candidate_by_id.get(cid)
            if ref is None:
                self.delayed_candidate_ids.discard(cid)
                continue
            next_attempt = parse_iso(ref.payload.get("next_attempt_at"))
            page_state = str((self.page_cache.get(ref.page_path) or {}).get("state") or "")
            if (not next_attempt or next_attempt <= now_dt) and page_state in {
                "cursor_committed", "draining"
            }:
                queue = self.claimable_by_keyword.setdefault(ref.keyword_id, deque())
                expected_profile_hash = self.active_profile_hashes.get(ref.keyword_id)
                if (
                    expected_profile_hash
                    and _relevance_claimable(ref.payload, expected_profile_hash)
                    and cid not in queue
                ):
                    queue.append(cid)
                self.delayed_candidate_ids.discard(cid)

    def apply_relevance_updates(
        self, page_path: Path, candidates: Iterable[Mapping[str, Any]],
    ) -> None:
        """Incrementally publish persisted deferred-relevance decisions."""
        for candidate in candidates:
            self.update_candidate(page_path, candidate)
            self.relevance_updates += 1

    def assert_active_bindings(self, expected: Mapping[str, str]) -> None:
        materialized = {str(key): str(value) for key, value in expected.items()}
        if self.active_profile_hashes != materialized:
            self.binding_invariant_failures += 1
            raise RuntimeError("journal index active relevance bindings drifted")


def _candidate_doi(item: Mapping[str, Any]) -> str:
    payload = item.get("candidate") if isinstance(item.get("candidate"), dict) else {}
    return normalize_doi(payload.get("doi") or "")


def _relevance_state(item: Mapping[str, Any]) -> str:
    """Return the orthogonal relevance state for one candidate.

    Journals written before relevance was introduced are interpreted as
    already claimable legacy observations.  Active profile-bound pages always
    carry an explicit ``profile_unbound`` record before cursor commit.
    """
    relevance = item.get("relevance")
    if isinstance(relevance, Mapping):
        return str(relevance.get("state") or "profile_unbound")
    return "passed"


def _relevance_claimable(
    item: Mapping[str, Any], expected_profile_hash: str | None = None,
) -> bool:
    if _relevance_state(item) not in RELEVANCE_CLAIMABLE_STATES:
        return False
    if expected_profile_hash is None:
        return True
    relevance = item.get("relevance")
    return bool(
        isinstance(relevance, Mapping)
        and relevance.get("profile_hash") == expected_profile_hash
    )


def _serialized_page_bytes(data: Mapping[str, Any]) -> bytes:
    return json.dumps(
        data, ensure_ascii=False, indent=2, sort_keys=False,
    ).encode("utf-8")


def transform_page_for_profile_closure(
    page_bytes: bytes,
    *,
    planned_mutations: tuple[Mapping[str, Any], ...],
    closure_timestamp: str,
    transaction_id: str,
    reason: RelevanceReason,
    target_profile_hash: str,
) -> bytes:
    """Pure, byte-deterministic stale-profile page transformation."""
    try:
        raw = json.loads(page_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JournalCorruptError("profile closure page bytes are invalid JSON") from exc
    data = validate_page(raw)
    by_id = {
        str(item.get("candidate_id") or ""): item
        for item in planned_mutations
    }
    if "" in by_id or len(by_id) != len(planned_mutations):
        raise InvalidStateTransition("planned profile mutations need unique candidate IDs")
    seen: set[str] = set()
    for item in data["candidates"]:
        cid = str(item.get("candidate_id") or "")
        mutation = by_id.get(cid)
        if mutation is None:
            continue
        if not is_profile_closeable_candidate(
            item, target_profile_hash=target_profile_hash,
        ):
            raise InvalidStateTransition(
                f"candidate {cid} is no longer profile-closeable"
            )
        relevance = item.get("relevance")
        old_hash = (
            str(relevance.get("profile_hash") or "")
            if isinstance(relevance, Mapping) else ""
        )
        item["relevance"] = {
            "state": "rejected",
            "profile_hash": old_hash,
            "matched_groups": {},
            "negative_matches": [],
            "reason": reason.value,
            "verification": {
                "status": "profile_superseded",
                "superseded_by_profile_hash": target_profile_hash,
                "profile_transaction_id": transaction_id,
                "profile_mutation_id": str(mutation.get("mutation_id") or ""),
            },
            "attempt_count": int(
                relevance.get("attempt_count") or 0
            ) if isinstance(relevance, Mapping) else 0,
            "next_retry_at": None,
            "last_attempt_at": closure_timestamp,
            "last_error_class": None,
            "last_http_status": None,
        }
        seen.add(cid)
    missing = sorted(set(by_id) - seen)
    if missing:
        raise KeyError("planned profile candidates missing: " + ",".join(missing))
    if _all_terminal(data["candidates"]) and data["state"] in {
        "cursor_committed", "draining",
    }:
        data["state"] = "drained"
        data["drained_at"] = data.get("drained_at") or closure_timestamp
    data["statistics"] = _statistics(data["candidates"])
    validate_page(data)
    return _serialized_page_bytes(data)


class PageJournalStore:
    """File-backed page journal store.

    Lock order rule for candidate drain: acquire page locks only for short
    claim/commit mutations. Never wait for DOI, resolution, export, or
    ``paper_raw`` locks while holding a page lock.
    """

    def __init__(self, root_dir: str | Path):
        self.root_dir = Path(root_dir)

    def page_path(
        self,
        *,
        keyword_id: str,
        query_id: str,
        provider: str,
        lane: PageLane,
        page_id: str,
    ) -> Path:
        return self.root_dir / keyword_id / query_id / provider / lane / f"{page_id}.json"

    @staticmethod
    def lock_for(path: Path) -> FileLock:
        return FileLock(str(path.with_suffix(path.suffix + ".lock")))

    def read(self, path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise JournalCorruptError(f"journal JSON corrupt: {path}: {exc}") from exc
        return _validate_page(data, path)

    def write_page(self, page: dict[str, Any]) -> Path:
        page = validate_page(page)
        path = self.page_path(
            keyword_id=page["keyword_id"],
            query_id=page["query_id"],
            provider=page["provider"],
            lane=page["lane"],
            page_id=page["page_id"],
        )
        with self.lock_for(path):
            if path.exists():
                existing = self.read(path)
                if existing.get("request_signature") != page.get("request_signature"):
                    raise JournalCorruptError(f"page_id collision with different signature: {path}")
                return path
            _atomic_write_json_unlocked(path, _validate_page(page, path))
        return path

    def make_page(
        self,
        *,
        page_id: str,
        keyword_id: str,
        keyword_zh: str,
        query_id: str,
        query: str,
        query_language: str,
        provider: str,
        lane: PageLane,
        request_signature_value: dict[str, Any],
        request_cursor: str | None,
        next_cursor: str | None,
        provider_exhausted: bool,
        candidates: list[PaperCandidate],
        generation: int = 1,
        refresh_run_id: str | None = None,
        page_sequence: int | None = None,
        state: PageState = "fetched",
        relevance_profile_hash: str | None = None,
    ) -> dict[str, Any]:
        now = now_iso()
        records = [
            _candidate_record(
                page_id, cand, idx, relevance_profile_hash=relevance_profile_hash
            )
            for idx, cand in enumerate(candidates)
        ]
        return {
            "schema_version": PAGE_SCHEMA_VERSION,
            "page_id": page_id,
            "keyword_id": keyword_id,
            "keyword_zh": keyword_zh,
            "query_id": query_id,
            "query": query,
            "query_language": query_language,
            "provider": provider,
            "lane": lane,
            "generation": int(generation),
            "refresh_run_id": refresh_run_id,
            "page_sequence": page_sequence,
            "request_signature": request_signature_value,
            "request_cursor": request_cursor,
            "next_cursor": next_cursor,
            "provider_exhausted": bool(provider_exhausted),
            "state": state,
            "fetched_at": now,
            "cursor_committed_at": now if state == "cursor_committed" else None,
            "drained_at": None,
            "candidates": records,
            "statistics": _statistics(records),
        }

    def transition_page(self, path: Path, new_state: PageState) -> dict[str, Any]:
        with self.lock_for(path):
            data = self.read(path)
            old = data["state"]
            if new_state not in _PAGE_TRANSITIONS[old]:
                raise InvalidStateTransition(f"page {old} -> {new_state} is not allowed")
            data["state"] = new_state
            if new_state == "cursor_committed":
                data["cursor_committed_at"] = now_iso()
            if new_state == "drained":
                data["drained_at"] = now_iso()
            data["statistics"] = _statistics(data["candidates"])
            _atomic_write_json_unlocked(path, data)
            return data

    def mark_cursor_committed(self, path: Path) -> dict[str, Any]:
        with self.lock_for(path):
            data = self.read(path)
            if data["state"] == "cursor_committed":
                return data
            if data["state"] != "fetched":
                raise InvalidStateTransition(f"cannot mark cursor_committed from {data['state']}")
            data["state"] = "cursor_committed"
            data["cursor_committed_at"] = now_iso()
            # A profile-bound page with only rejected/invalid relevance
            # decisions has no candidate work left to drain.  Preserve the
            # legacy cursor-commit state for pre-profile pages, but close
            # explicit relevance pages immediately.
            if (
                data["candidates"]
                and all(isinstance(item.get("relevance"), Mapping) for item in data["candidates"])
                and _all_terminal(data["candidates"])
            ):
                data["state"] = "drained"
                data["drained_at"] = data.get("drained_at") or now_iso()
            data["statistics"] = _statistics(data["candidates"])
            _atomic_write_json_unlocked(path, data)
            return data

    def finalize_relevance(
        self,
        path: Path,
        decisions: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Persist notebook-local relevance decisions before cursor CAS.

        This mutation is intentionally valid only while a page is ``fetched``.
        A second invocation is idempotent for already materialized decisions,
        but it refuses to rewrite a page whose cursor has already advanced.
        Candidate lifecycle fields are never changed here.
        """
        with self.lock_for(path):
            data = self.read(path)
            if data["state"] != "fetched":
                if data["state"] in {"cursor_committed", "draining", "drained"}:
                    return data
                raise InvalidStateTransition(
                    f"cannot finalize relevance for page state {data['state']}"
                )
            by_id = {str(key): value for key, value in decisions.items()}
            seen: set[str] = set()
            for item in data["candidates"]:
                cid = str(item.get("candidate_id") or "")
                decision = by_id.get(cid)
                if decision is None:
                    continue
                if not isinstance(decision, Mapping):
                    raise JournalCorruptError(f"relevance decision must be object: {path}")
                new_state = str(decision.get("state") or "")
                if new_state not in RELEVANCE_STATES or new_state == "profile_unbound":
                    raise InvalidStateTransition(f"invalid relevance decision {new_state!r}")
                old_state = (
                    _relevance_state(item)
                    if isinstance(item.get("relevance"), Mapping)
                    else "profile_unbound"
                )
                allowed = {
                    "profile_unbound": RELEVANCE_STATES - {"profile_unbound"},
                    "verification_deferred": RELEVANCE_STATES - {"profile_unbound"},
                    "passed": {"passed"},
                    "rejected": {"rejected"},
                    "candidate_invalid": {"candidate_invalid"},
                }.get(old_state, set())
                if new_state not in allowed:
                    raise InvalidStateTransition(
                        f"relevance {old_state} -> {new_state} is not allowed"
                    )
                materialized = dict(decision)
                materialized.setdefault("profile_hash", "")
                materialized.setdefault("matched_groups", {})
                materialized.setdefault("negative_matches", [])
                materialized.setdefault("reason", "")
                materialized.setdefault("verification", {})
                materialized.setdefault("attempt_count", 0)
                materialized.setdefault("next_retry_at", None)
                materialized.setdefault("last_attempt_at", None)
                materialized.setdefault("last_error_class", None)
                materialized.setdefault("last_http_status", None)
                old_relevance = item.get("relevance")
                old_profile_hash = (
                    str(old_relevance.get("profile_hash") or "")
                    if isinstance(old_relevance, Mapping) else ""
                )
                decision_profile_hash = str(materialized.get("profile_hash") or "")
                if old_profile_hash and decision_profile_hash != old_profile_hash:
                    raise InvalidStateTransition(
                        f"relevance profile hash changed for candidate {cid}"
                    )
                item["relevance"] = materialized
                seen.add(cid)
            unknown = sorted(set(by_id) - seen)
            if unknown:
                raise KeyError(f"relevance decisions reference unknown candidates: {unknown}")
            still_unbound = sorted(
                str(item.get("candidate_id") or "")
                for item in data["candidates"]
                if _relevance_state(item) == "profile_unbound"
            )
            if still_unbound:
                raise InvalidStateTransition(
                    "relevance finalization left profile_unbound candidates: "
                    + ",".join(still_unbound)
                )
            data["statistics"] = _statistics(data["candidates"])
            _atomic_write_json_unlocked(path, data)
            return data

    def close_stale_profile_candidates(
        self,
        path: Path,
        *,
        new_profile_hash: str,
        planned_mutations: tuple[Mapping[str, Any], ...],
        closure_timestamp: str,
        transaction_id: str,
        reason: RelevanceReason = RelevanceReason.STALE_PROFILE_CLOSED_BY_PROFILE_APPLY,
    ) -> dict[str, Any]:
        """Reject every nonterminal relevance verdict from an older profile.

        Page ``generation`` belongs to the provider lane and is deliberately
        not consulted here.  Candidate receipts and the request signature are
        the profile identity facts.
        """
        with self.lock_for(path):
            transformed = transform_page_for_profile_closure(
                path.read_bytes(),
                planned_mutations=planned_mutations,
                closure_timestamp=closure_timestamp,
                transaction_id=transaction_id,
                reason=reason,
                target_profile_hash=new_profile_hash,
            )
            data = json.loads(transformed.decode("utf-8"))
            atomic_replace_bytes_unlocked(path, transformed)
            return data

    def retry_deferred_relevance(
        self,
        path: Path,
        decisions: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Update due deferred decisions without reopening a committed page."""
        with self.lock_for(path):
            data = self.read(path)
            by_id = {str(key): value for key, value in decisions.items()}
            for item in data["candidates"]:
                cid = str(item.get("candidate_id") or "")
                if cid not in by_id:
                    continue
                if _relevance_state(item) != "verification_deferred":
                    continue
                decision = by_id[cid]
                state = str(decision.get("state") or "")
                if state not in RELEVANCE_STATES - {"profile_unbound"}:
                    raise InvalidStateTransition(f"invalid deferred relevance state {state!r}")
                old_hash = str((item.get("relevance") or {}).get("profile_hash") or "")
                new_hash = str(decision.get("profile_hash") or "")
                if old_hash and old_hash != new_hash:
                    raise InvalidStateTransition(
                        f"deferred relevance profile hash changed for candidate {cid}"
                    )
                item["relevance"] = dict(decision)
            data["statistics"] = _statistics(data["candidates"])
            if _all_terminal(data["candidates"]) and data["state"] in {"cursor_committed", "draining"}:
                data["state"] = "drained"
                data["drained_at"] = data.get("drained_at") or now_iso()
            _atomic_write_json_unlocked(path, data)
            return data

    def list_pages(self, keyword_ids: Iterable[str] | None = None) -> list[PageRef]:
        if not self.root_dir.exists():
            return []
        wanted = set(keyword_ids or [])
        refs: list[PageRef] = []
        for path in sorted(self.root_dir.glob("*/*/*/*/*.json")):
            try:
                data = self.read(path)
            except JournalCorruptError:
                raise
            if wanted and data.get("keyword_id") not in wanted:
                continue
            refs.append(PageRef(
                path=path,
                page_id=data["page_id"],
                keyword_id=data["keyword_id"],
                query_id=data["query_id"],
                provider=data["provider"],
                lane=data["lane"],
                state=data["state"],
                fetched_at=data.get("fetched_at") or "",
            ))
        refs.sort(key=lambda r: (
            0 if r.state in {"cursor_committed", "draining"} else 1,
            0 if r.lane == "backfill" else 1,
            r.fetched_at,
            str(r.path),
        ))
        return refs

    def count_pending_candidates(self, keyword_ids: Iterable[str] | None = None) -> int:
        count = 0
        for ref in self.list_pages(keyword_ids):
            data = self.read(ref.path)
            for item in data["candidates"]:
                if _relevance_claimable(item) and item.get("status") in NONTERMINAL_CANDIDATE_STATES:
                    count += 1
        return count

    def claim_candidate(
        self,
        page_path: Path,
        *,
        candidate_id_value: str,
        worker_id: str,
        lease_seconds: int,
    ) -> ClaimResult:
        with self.lock_for(page_path):
            data = self.read(page_path)
            if data["state"] not in {"cursor_committed", "draining", "drained"}:
                return ClaimResult(
                    False,
                    page_path,
                    candidate_id_value,
                    reason=f"page_not_claimable:{data['state']}",
                )
            if data["state"] == "drained":
                return ClaimResult(False, page_path, candidate_id_value, reason="page_drained")
            if data["state"] == "cursor_committed":
                data["state"] = "draining"
            now_dt = datetime.now(timezone.utc)
            for item in data["candidates"]:
                if item.get("candidate_id") != candidate_id_value:
                    continue
                status = item.get("status")
                expires = parse_iso(item.get("lease_expires_at"))
                next_attempt = parse_iso(item.get("next_attempt_at"))
                if next_attempt and next_attempt > now_dt:
                    return ClaimResult(False, page_path, candidate_id_value, reason="deferred_until_next_attempt")
                if status == "processing" and expires and expires > now_dt:
                    return ClaimResult(False, page_path, candidate_id_value, reason="lease_active")
                if not _relevance_claimable(item):
                    return ClaimResult(False, page_path, candidate_id_value, reason="relevance_not_passed")
                if status not in {"pending", "ready", "failed_retryable", "processing"}:
                    return ClaimResult(False, page_path, candidate_id_value, reason=f"not_claimable:{status}")
                _transition_candidate(item, "processing")
                item["claimed_by"] = worker_id
                item["claimed_at"] = now_iso()
                item["lease_expires_at"] = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
                item["attempts"] = int(item.get("attempts") or 0) + 1
                data["statistics"] = _statistics(data["candidates"])
                _atomic_write_json_unlocked(page_path, data)
                return ClaimResult(True, page_path, candidate_id_value, candidate=dict(item))
        return ClaimResult(False, page_path, candidate_id_value, reason="candidate_not_found")

    def claim_candidates_from_page(self, page_path: Path, *, worker_id: str,
                                   lease_seconds: int, limit: int = 16,
                                   candidate_ids: Iterable[str] | None = None,
                                   expected_profile_hash: str | None = None) -> list[CandidateClaim]:
        """Claim up to ``limit`` candidates with one page read and one fsync."""
        if limit < 1:
            return []
        wanted = set(candidate_ids or [])
        claims: list[CandidateClaim] = []
        with self.lock_for(page_path):
            data = self.read(page_path)
            if data["state"] not in {"cursor_committed", "draining"}:
                return claims
            now_dt = datetime.now(timezone.utc)
            if data["state"] == "cursor_committed":
                data["state"] = "draining"
            for item in data["candidates"]:
                if len(claims) >= limit:
                    break
                cid = str(item.get("candidate_id") or "")
                if wanted and cid not in wanted:
                    continue
                expires = parse_iso(item.get("lease_expires_at"))
                next_attempt = parse_iso(item.get("next_attempt_at"))
                status = item.get("status")
                if not _relevance_claimable(item, expected_profile_hash):
                    continue
                if next_attempt and next_attempt > now_dt:
                    continue
                if status == "processing" and expires and expires > now_dt:
                    continue
                if status not in {"pending", "ready", "failed_retryable", "processing"}:
                    continue
                _transition_candidate(item, "processing")
                item["claimed_by"] = worker_id
                item["claimed_at"] = now_iso()
                item["lease_expires_at"] = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
                item["attempts"] = int(item.get("attempts") or 0) + 1
                claims.append(CandidateClaim(
                    cid, str(data["keyword_id"]), str(data["page_id"]),
                    str(data["provider"]), _candidate_doi(item), page_path,
                    dict(item), str(item["lease_expires_at"])))
            if claims:
                data["statistics"] = _statistics(data["candidates"])
                _atomic_write_json_unlocked(page_path, data)
        return claims

    def renew_candidate_lease(
        self,
        page_path: Path,
        *,
        candidate_id_value: str,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        with self.lock_for(page_path):
            data = self.read(page_path)
            for item in data["candidates"]:
                if item.get("candidate_id") == candidate_id_value:
                    if item.get("status") != "processing" or item.get("claimed_by") != worker_id:
                        return False
                    item["lease_expires_at"] = (
                        datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
                    ).isoformat()
                    _atomic_write_json_unlocked(page_path, data)
                    return True
        return False

    def defer_candidate(
        self,
        page_path: Path,
        *,
        candidate_id_value: str,
        worker_id: str,
        reason: str,
        drain_generation: str = "",
        next_attempt_at: str | None = None,
        updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Release a claimed candidate as retryable without making it terminal.

        Used when another observation only has a temporary DOI claim, or when a
        formal workspace needs repair outside the discovery drain loop. The
        claim owner check mirrors ``commit_candidate`` so an unrelated worker
        cannot steal or release another active lease.
        """
        with self.lock_for(page_path):
            data = self.read(page_path)
            for item in data["candidates"]:
                if item.get("candidate_id") != candidate_id_value:
                    continue
                if item.get("status") != "processing" or item.get("claimed_by") != worker_id:
                    raise InvalidStateTransition("only claim owner may defer processing candidate")
                _transition_candidate(item, "failed_retryable")
                item["claimed_by"] = None
                item["claimed_at"] = None
                item["lease_expires_at"] = None
                item["last_deferred_reason"] = reason
                if drain_generation:
                    item["deferred_generation"] = drain_generation
                if next_attempt_at:
                    item["next_attempt_at"] = next_attempt_at
                elif "next_attempt_at" in item:
                    item.pop("next_attempt_at", None)
                if updates:
                    item.update(updates)
                data["statistics"] = _statistics(data["candidates"])
                _atomic_write_json_unlocked(page_path, data)
                return dict(item)
        raise KeyError(f"candidate not found: {candidate_id_value}")

    def commit_candidate(
        self,
        page_path: Path,
        *,
        candidate_id_value: str,
        worker_id: str,
        new_status: CandidateState,
        updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.lock_for(page_path):
            data = self.read(page_path)
            for item in data["candidates"]:
                if item.get("candidate_id") != candidate_id_value:
                    continue
                if item.get("status") == "processing" and item.get("claimed_by") != worker_id:
                    raise InvalidStateTransition("only claim owner may commit processing result")
                _transition_candidate(item, new_status)
                if updates:
                    item.update(updates)
                if new_status in TERMINAL_CANDIDATE_STATES or new_status == "failed_retryable":
                    item["claimed_by"] = None
                    item["claimed_at"] = None
                    item["lease_expires_at"] = None
                data["statistics"] = _statistics(data["candidates"])
                if _all_terminal(data["candidates"]):
                    if data["state"] != "drained":
                        if data["state"] not in {"cursor_committed", "draining"}:
                            raise InvalidStateTransition(f"cannot drain page from {data['state']}")
                        data["state"] = "drained"
                        data["drained_at"] = now_iso()
                _atomic_write_json_unlocked(page_path, data)
                return dict(item)
        raise KeyError(f"candidate not found: {candidate_id_value}")

    def commit_candidate_results(self, page_path: Path, results: Iterable[Mapping[str, Any]],
                                 *, worker_id: str) -> list[dict[str, Any]]:
        """Merge multiple candidate outcomes into one atomic page write."""
        by_id = {str(result["candidate_id"]): result for result in results}
        if not by_id:
            return []
        committed: list[dict[str, Any]] = []
        with self.lock_for(page_path):
            data = self.read(page_path)
            for item in data["candidates"]:
                result = by_id.get(str(item.get("candidate_id") or ""))
                if result is None:
                    continue
                if item.get("status") == "processing" and item.get("claimed_by") != worker_id:
                    raise InvalidStateTransition("only claim owner may commit processing result")
                new_status = str(result["new_status"])
                _transition_candidate(item, new_status)  # type: ignore[arg-type]
                updates = result.get("updates")
                if isinstance(updates, Mapping):
                    item.update(updates)
                if new_status in TERMINAL_CANDIDATE_STATES or new_status == "failed_retryable":
                    item["claimed_by"] = None
                    item["claimed_at"] = None
                    item["lease_expires_at"] = None
                committed.append(dict(item))
            if len(committed) != len(by_id):
                missing = sorted(set(by_id) - {str(item["candidate_id"]) for item in committed})
                raise KeyError(f"candidates not found: {','.join(missing)}")
            data["statistics"] = _statistics(data["candidates"])
            if _all_terminal(data["candidates"]):
                if data["state"] not in {"cursor_committed", "draining", "drained"}:
                    raise InvalidStateTransition(f"cannot drain page from {data['state']}")
                data["state"] = "drained"
                data["drained_at"] = data.get("drained_at") or now_iso()
            _atomic_write_json_unlocked(page_path, data)
        return committed

    def update_candidate_payload(
        self,
        page_path: Path,
        *,
        candidate_id_value: str,
        worker_id: str,
        candidate_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Update a claimed candidate payload without changing its identity/state.

        Title resolution can enrich a no-DOI observation after the candidate has
        been claimed. The candidate_id is intentionally stable: receipts,
        leases, and recovery records all key off the original observation.
        """
        with self.lock_for(page_path):
            data = self.read(page_path)
            for item in data["candidates"]:
                if item.get("candidate_id") != candidate_id_value:
                    continue
                if item.get("status") != "processing" or item.get("claimed_by") != worker_id:
                    raise InvalidStateTransition("only claim owner may update candidate payload")
                item["candidate"] = dict(candidate_payload)
                _validate_page(data, page_path)
                _atomic_write_json_unlocked(page_path, data)
                return dict(item)
        raise KeyError(f"candidate not found: {candidate_id_value}")

    def iter_claimable(self, keyword_ids: Iterable[str] | None = None) -> list[tuple[Path, dict[str, Any]]]:
        now_dt = datetime.now(timezone.utc)
        out: list[tuple[Path, dict[str, Any]]] = []
        for ref in self.list_pages(keyword_ids):
            if ref.state not in {"cursor_committed", "draining"}:
                continue
            data = self.read(ref.path)
            for item in data["candidates"]:
                status = item.get("status")
                if not _relevance_claimable(item):
                    continue
                expires = parse_iso(item.get("lease_expires_at"))
                next_attempt = parse_iso(item.get("next_attempt_at"))
                if next_attempt and next_attempt > now_dt:
                    continue
                if _relevance_claimable(item) and status in {"pending", "ready", "failed_retryable"}:
                    out.append((ref.path, dict(item)))
                elif _relevance_claimable(item) and status == "processing" and (not expires or expires <= now_dt):
                    out.append((ref.path, dict(item)))
        return out


def _transition_candidate(item: dict[str, Any], new_state: CandidateState) -> None:
    old = item.get("status")
    if new_state not in _CANDIDATE_TRANSITIONS.get(old, set()):
        if old == new_state:
            return
        raise InvalidStateTransition(f"candidate {old} -> {new_state} is not allowed")
    item["status"] = new_state


def _all_terminal(candidates: list[dict[str, Any]]) -> bool:
    return all(
        item.get("status") in TERMINAL_CANDIDATE_STATES
        or _relevance_state(item) in RELEVANCE_TERMINAL_STATES
        for item in candidates
    )


def _statistics(candidates: list[dict[str, Any]]) -> dict[str, int]:
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
    for item in candidates:
        status = str(item.get("status") or "")
        relevance_state = _relevance_state(item)
        relevance_key = f"relevance_{relevance_state}"
        if relevance_key in stats:
            stats[relevance_key] += 1
        if status in TERMINAL_CANDIDATE_STATES or relevance_state in RELEVANCE_TERMINAL_STATES:
            stats["terminal"] += 1
        else:
            if relevance_state == "passed":
                stats["pending"] += 1
        if status in stats:
            stats[status] += 1
        if status == "invalid_doi":
            stats["invalid"] += 1
    return stats
