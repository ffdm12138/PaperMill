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
from src.discovery.execution.lane_models import (
    DiscoveryLaneKey,
    DurableProviderPage,
    ExhaustionEvidence,
    ProviderResponseMetadata,
    RequestSignature,
)
from src.discovery.models import PaperCandidate, normalize_doi, normalize_title
from src.discovery.relevance import (
    RELEVANCE_REASON_VALUES,
    RELEVANCE_STATES,
    RelevanceReason,
    RelevanceState,
)
from src.utils.atomic_io import atomic_replace_bytes_unlocked


from src.discovery.constants import INITIAL_CURSOR


PAGE_SCHEMA_VERSION = "4.0"

# The v4 journal is the active provider-page format.  V4 adds 'checksum'
# and 'lane_key' typed fields vs v3.  Old PAGE_V3_FIELDS retained for
# compatibility with legacy page journals that predate checksum support.
# See contracts/page_journal.py for the strict canonical PAGE_V4_FIELDS.
PAGE_V3_FIELDS: frozenset[str] = frozenset({
    "schema_version", "page_id", "keyword_id", "keyword_zh",
    "query_id", "query", "query_language", "provider", "lane",
    "generation", "request_signature",
    "request_cursor", "next_cursor",
    "provider_exhausted", "returned_count", "lane_key",
    "response_metadata", "exhaustion_evidence", "state",
    "fetched_at", "cursor_committed_at", "drained_at",
    "candidates", "statistics",
    "refresh_run_id", "page_sequence",
})

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

RELEVANCE_TERMINAL_STATES = {RelevanceState.REJECTED, RelevanceState.CANDIDATE_INVALID}
RELEVANCE_CLAIMABLE_STATES = frozenset({RelevanceState.PASSED})
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


def _path_is_reparse(path: Path) -> bool:
    """Detect symlinks and Windows reparse points without following them."""
    try:
        info = path.lstat()
    except OSError:
        return True  # cannot stat → treat as unsafe
    if hasattr(os.path, 'islink') and os.path.islink(path):  # noqa: PTH111
        return True
    import stat as _stat_mod
    attrs = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(_stat_mod, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attrs & reparse_flag)


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
    """Return the canonical complete request signature dictionary.

    ``RequestSignature`` is the single authority for the digest and exact
    field set.  The journal keeps this small dict representation solely for
    JSON persistence.
    """
    from src.discovery.execution.lane_models import RequestSignature

    return RequestSignature.create(
        sort=sort,
        filters=filters,
        page_size=page_size,
        pagination_schema_version=pagination_schema_version,
    ).to_dict()


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
    """Strictly validate one active complete schema-v3 provider-page journal."""
    if not isinstance(data, dict):
        raise JournalCorruptError(f"journal root is not object: {path or ''}")
    missing = sorted(PAGE_V3_FIELDS - set(data))
    if missing:
        raise JournalCorruptError(f"journal missing keys {missing}: {path or ''}")
    unexpected = sorted(set(data) - PAGE_V3_FIELDS)
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
    try:
        typed_signature = RequestSignature.from_dict_strict(signature)
    except (TypeError, ValueError) as exc:
        raise JournalCorruptError(f"journal request_signature is invalid: {path or ''}") from exc
    if signature != typed_signature.to_dict():
        raise JournalCorruptError(f"journal request_signature hash/content mismatch: {path or ''}")
    lane_key_data = data.get("lane_key")
    if not isinstance(lane_key_data, dict):
        raise JournalCorruptError(f"journal lane_key must be object: {path or ''}")
    try:
        lane_key = DiscoveryLaneKey.from_dict_strict(lane_key_data)
    except (TypeError, ValueError) as exc:
        raise JournalCorruptError(f"journal lane_key is invalid: {path or ''}") from exc
    expected_lane_key = {
        "keyword_id": data["keyword_id"],
        "query_id": data["query_id"],
        "provider": data["provider"],
        "mode": data["lane"],
        "generation": generation,
        "request_signature": typed_signature.hash,
    }
    if lane_key.to_dict() != expected_lane_key:
        raise JournalCorruptError(f"journal lane_key does not match page identity: {path or ''}")
    if not isinstance(data.get("request_cursor"), str):
        raise JournalCorruptError(
            f"journal request_cursor must be a concrete string: {path or ''}"
        )
    if data.get("next_cursor") is not None and not isinstance(data.get("next_cursor"), str):
        raise JournalCorruptError(f"journal next_cursor must be string or null: {path or ''}")
    if not isinstance(data.get("provider_exhausted"), bool):
        raise JournalCorruptError(f"journal provider_exhausted must be boolean: {path or ''}")
    returned_count = data.get("returned_count")
    if isinstance(returned_count, bool) or not isinstance(returned_count, int) or returned_count < 0:
        raise JournalCorruptError(f"journal returned_count must be a non-negative integer: {path or ''}")
    response_metadata = data.get("response_metadata")
    if not isinstance(response_metadata, dict):
        raise JournalCorruptError(f"journal response_metadata must be object: {path or ''}")
    try:
        typed_metadata = ProviderResponseMetadata.from_dict_strict(response_metadata)
    except (TypeError, ValueError) as exc:
        raise JournalCorruptError(f"journal response_metadata is incomplete or invalid: {path or ''}") from exc
    if typed_metadata.next_cursor_present != bool(data.get("next_cursor")):
        raise JournalCorruptError(f"journal response_metadata next_cursor mismatch: {path or ''}")
    evidence_data = data.get("exhaustion_evidence")
    if data["provider_exhausted"]:
        if not isinstance(evidence_data, dict):
            raise JournalCorruptError(f"exhausted journal lacks durable exhaustion_evidence: {path or ''}")
        try:
            evidence = ExhaustionEvidence.from_dict_strict(evidence_data)
        except (TypeError, ValueError) as exc:
            raise JournalCorruptError(f"journal exhaustion_evidence is invalid: {path or ''}") from exc
        if (
            evidence.provider != data["provider"]
            or evidence.query_id != data["query_id"]
            or evidence.request_signature != typed_signature.hash
            or evidence.generation != generation
            or evidence.cursor_before != data["request_cursor"]
            or evidence.response_metadata != typed_metadata
        ):
            raise JournalCorruptError(f"journal exhaustion_evidence does not bind page identity: {path or ''}")
    elif evidence_data is not None:
        raise JournalCorruptError(f"non-exhausted journal must not carry exhaustion_evidence: {path or ''}")
    for field_name in ("fetched_at", "cursor_committed_at", "drained_at"):
        value = data.get(field_name)
        if value is not None and not isinstance(value, str):
            raise JournalCorruptError(f"journal {field_name} must be string or null: {path or ''}")
    if not isinstance(data.get("candidates"), list):
        raise JournalCorruptError(f"journal candidates must be list: {path or ''}")
    if returned_count != len(data["candidates"]):
        raise JournalCorruptError(f"journal returned_count does not match candidates: {path or ''}")
    seen_candidate_ids: set[str] = set()
    for item in data["candidates"]:
        if not isinstance(item, dict) or not isinstance(item.get("candidate_id"), str) or not item.get("candidate_id"):
            raise JournalCorruptError(f"invalid candidate record: {path or ''}")
        candidate_id_value = str(item["candidate_id"])
        if candidate_id_value in seen_candidate_ids:
            raise JournalCorruptError(
                f"duplicate candidate_id {candidate_id_value!r}: {path or ''}"
            )
        seen_candidate_ids.add(candidate_id_value)
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


def select_stable_emitted_primary(
    current: EmittedPrimaryRef | None,
    candidate: EmittedPrimaryRef,
) -> EmittedPrimaryRef:
    """Return the single stable emitted primary for a DOI.

    The sort key is ``candidate_id`` — a deterministic, cross-restart
    value derived from ``stable_hash(page_id, provider_record_id)``.
    Neither traversal order, dict insertion order, filesystem glob
    order, nor wall-clock time may influence the selection.
    """
    if current is None:
        return candidate
    # Lexicographic by candidate_id — stable and reproducible.
    if candidate.candidate_id < current.candidate_id:
        return candidate
    return current


def page_is_drain_visible(page: Mapping[str, Any]) -> bool:
    """Return ``True`` when a page's candidates may enter the drain queue.

    Only ``cursor_committed`` and ``draining`` pages are visible.
    ``fetched``, ``fetching``, ``planned``, ``failed``, and any unknown
    state are excluded — even when individual candidates carry a
    ``passed`` relevance verdict and the correct ``profile_hash``.
    """
    return str(page.get("state") or "") in {"cursor_committed", "draining"}


class _IndexState:
    """Immutable snapshot of all index projections — swapped atomically."""
    __slots__ = (
        "candidate_by_id", "claimable_by_keyword", "processing_by_doi",
        "emitted_by_doi", "terminal_by_doi", "page_cache", "delayed_candidate_ids",
    )

    def __init__(
        self,
        candidate_by_id: dict[str, CandidateRef] | None = None,
        claimable_by_keyword: dict[str, deque[str]] | None = None,
        processing_by_doi: dict[str, str] | None = None,
        emitted_by_doi: dict[str, EmittedPrimaryRef] | None = None,
        terminal_by_doi: dict[str, list[str]] | None = None,
        page_cache: dict[Path, dict[str, Any]] | None = None,
        delayed_candidate_ids: set[str] | None = None,
    ):
        self.candidate_by_id: dict[str, CandidateRef] = candidate_by_id or {}
        self.claimable_by_keyword: dict[str, deque[str]] = claimable_by_keyword or {}
        self.processing_by_doi: dict[str, str] = processing_by_doi or {}
        self.emitted_by_doi: dict[str, EmittedPrimaryRef] = emitted_by_doi or {}
        self.terminal_by_doi: dict[str, list[str]] = terminal_by_doi or {}
        self.page_cache: dict[Path, dict[str, Any]] = page_cache or {}
        self.delayed_candidate_ids: set[str] = delayed_candidate_ids or set()

    def copy(self) -> "_IndexState":
        return _IndexState(
            candidate_by_id=dict(self.candidate_by_id),
            claimable_by_keyword={
                kw: deque(q) for kw, q in self.claimable_by_keyword.items()
            },
            processing_by_doi=dict(self.processing_by_doi),
            emitted_by_doi=dict(self.emitted_by_doi),
            terminal_by_doi={
                doi: list(cids) for doi, cids in self.terminal_by_doi.items()
            },
            page_cache=dict(self.page_cache),
            delayed_candidate_ids=set(self.delayed_candidate_ids),
        )


@dataclass
class JournalDrainIndex:
    """One full journal read per batch followed by in-memory lookups.

    All mutable projections are stored in a single ``_state``
    reference that is swapped atomically under ``_lock``.  External
    modules must use the reader accessor methods — never read fields
    directly.
    """
    _state: _IndexState = field(default_factory=_IndexState)
    dirty_pages: set[Path] = field(default_factory=set)
    emitted_validation_cache: dict[
        tuple[str, int, int, str, int, int], tuple[bool, str]
    ] = field(default_factory=dict)
    full_scans: int = 1
    pages_read: int = 0
    lookups: int = 0
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
        state = _IndexState()
        index = cls(
            _state=state, pages_read=0,
            active_profile_hashes=bindings,
        )
        if not store.root_dir.exists():
            return index
        now_dt = datetime.now(timezone.utc)
        cid_page_tracker: dict[str, Path] = {}
        for path in sorted(store.root_dir.glob("*/*/*/*/*.json")):
            page = store.read(path)
            index.pages_read += 1
            state.page_cache[path] = page
            keyword_id = str(page["keyword_id"])
            expected_profile_hash = bindings.get(keyword_id)
            queue = state.claimable_by_keyword.setdefault(keyword_id, deque())
            for item in page["candidates"]:
                cid = str(item.get("candidate_id") or "")
                if not cid:
                    continue
                # Cross-page candidate_id collision detection.
                existing_path = cid_page_tracker.get(cid)
                if existing_path is not None and existing_path != path:
                    raise JournalCorruptError(
                        f"candidate_id collision across pages: {cid} on "
                        f"{existing_path} and {path}"
                    )
                cid_page_tracker[cid] = path
                state.candidate_by_id[cid] = CandidateRef(cid, keyword_id, path, dict(item))
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
                drain_visible = page_is_drain_visible(page)
                if claimable and drain_visible:
                    queue.append(cid)
                elif (next_attempt and next_attempt > now_dt
                      and status in {"pending", "ready", "failed_retryable"}
                      and bool(expected_profile_hash)
                      and _relevance_claimable(item, expected_profile_hash)
                      and drain_visible):
                    state.delayed_candidate_ids.add(cid)
                if (
                    status == "processing" and doi and expires and expires > now_dt
                    and bool(expected_profile_hash)
                    and _relevance_claimable(item, expected_profile_hash)
                ):
                    state.processing_by_doi.setdefault(doi, cid)
                elif status == "emitted" and doi:
                    ref = EmittedPrimaryRef(cid, path, dict(item))
                    state.emitted_by_doi[doi] = select_stable_emitted_primary(
                        state.emitted_by_doi.get(doi), ref,
                    )
                if status in DURABLE_DOI_CANDIDATE_STATES and doi:
                    state.terminal_by_doi.setdefault(doi, []).append(cid)
        return index

    def claimable(self, keyword_ids: Iterable[str] | None = None) -> list[CandidateRef]:
        with self._lock:
            self._promote_due_candidates()
            self.lookups += 1
            wanted = set(keyword_ids or self._state.claimable_by_keyword)
            return [
                self._state.candidate_by_id[cid]
                for keyword in sorted(wanted)
                for cid in self._state.claimable_by_keyword.get(keyword, ())
                if _relevance_claimable(
                    self._state.candidate_by_id[cid].payload,
                    self.active_profile_hashes.get(keyword),
                )
            ]

    # ── Reader accessors (Phase 1.4) ────────────────────────────────────
    # External modules must use these instead of reading mutable fields
    # directly.  Each method either holds ``_lock`` or captures an
    # immutable snapshot.

    def get_candidate_ref(self, candidate_id: str) -> CandidateRef | None:
        """Return the :class:`CandidateRef` for *candidate_id* or ``None``."""
        with self._lock:
            return self._state.candidate_by_id.get(candidate_id)

    def get_emitted_primary(self, doi: str) -> EmittedPrimaryRef | None:
        """Return the stable emitted primary for *doi* or ``None``."""
        with self._lock:
            return self._state.emitted_by_doi.get(doi)

    def get_processing_owner(self, doi: str) -> str:
        """Return the candidate_id currently processing *doi* (``""`` if none)."""
        with self._lock:
            return self._state.processing_by_doi.get(doi, "")

    def has_page(self, page_path: Path) -> bool:
        """Return ``True`` when *page_path* is in the index cache."""
        with self._lock:
            return page_path in self._state.page_cache

    def get_page_keyword_id(self, page_path: Path) -> str:
        """Return the ``keyword_id`` for a cached page (``""`` if unknown)."""
        with self._lock:
            page = self._state.page_cache.get(page_path)
            return str(page.get("keyword_id") or "") if page is not None else ""

    def get_active_profile_hash(self, keyword_id: str) -> str | None:
        """Return the active profile hash for *keyword_id* or ``None``."""
        with self._lock:
            return self.active_profile_hashes.get(keyword_id)

    def page_count_for_keyword(self, keyword_id: str) -> int:
        """Return the number of cached pages belonging to *keyword_id*."""
        with self._lock:
            return sum(
                1 for page in self._state.page_cache.values()
                if page.get("keyword_id") == keyword_id
            )

    def get_cached_emitted_validation(
        self, key: tuple[str, int, int, str, int, int],
    ) -> tuple[bool, str] | None:
        """Return a cached emitted-validation result or ``None``."""
        with self._lock:
            return self.emitted_validation_cache.get(key)

    def set_cached_emitted_validation(
        self,
        key: tuple[str, int, int, str, int, int],
        value: tuple[bool, str],
        *,
        manifest_identity: str,
        jsonl_identity: str,
    ) -> None:
        """Store an emitted-validation result, evicting stale keys first."""
        with self._lock:
            for old_key in list(self.emitted_validation_cache):
                if old_key[0] == manifest_identity and old_key[3] == jsonl_identity:
                    self.emitted_validation_cache.pop(old_key, None)
            self.emitted_validation_cache[key] = value

    def add_page(self, path: Path, page: Mapping[str, Any]) -> None:
        """Publish a freshly persisted page as a copy-on-write replacement.

        All old projections for the same page are removed before the new ones
        are inserted.  Cross-page ``candidate_id`` collisions fail closed.
        On any error the index is left unchanged — the atomic ``_state``
        swap only happens after all validation passes.
        """
        materialized = dict(page)
        validate_page(materialized, path)
        with self._lock:
            keyword_id = str(page["keyword_id"])
            expected_profile_hash = self.active_profile_hashes.get(keyword_id)
            now_dt = datetime.now(timezone.utc)

            # ── Single copy-on-write clone of the entire state ──────────
            new_state = self._state.copy()

            # ── Remove old projections for this page ────────────────────
            old_cids = {
                cid for cid, ref in new_state.candidate_by_id.items()
                if ref.page_path == path
            }
            for cid in old_cids:
                del new_state.candidate_by_id[cid]
            for kw_queue in new_state.claimable_by_keyword.values():
                survivors = [cid for cid in kw_queue if cid not in old_cids]
                kw_queue.clear()
                kw_queue.extend(survivors)
            new_state.delayed_candidate_ids.difference_update(old_cids)
            for doi, cid in list(new_state.processing_by_doi.items()):
                if cid in old_cids:
                    del new_state.processing_by_doi[doi]
            for doi, ref in list(new_state.emitted_by_doi.items()):
                if ref.candidate_id in old_cids:
                    del new_state.emitted_by_doi[doi]
            for doi in list(new_state.terminal_by_doi):
                new_state.terminal_by_doi[doi] = [
                    cid for cid in new_state.terminal_by_doi[doi]
                    if cid not in old_cids
                ]
                if not new_state.terminal_by_doi[doi]:
                    del new_state.terminal_by_doi[doi]

            # ── Insert new page projections ─────────────────────────────
            new_state.page_cache[path] = materialized
            new_queue = new_state.claimable_by_keyword.setdefault(keyword_id, deque())
            drain_visible = page_is_drain_visible(page)
            for raw in page.get("candidates", []):
                item = dict(raw)
                cid = str(item.get("candidate_id") or "")
                if not cid:
                    continue
                existing = new_state.candidate_by_id.get(cid)
                if existing is not None and existing.page_path != path:
                    raise JournalCorruptError(
                        f"candidate_id collision: {cid} already on "
                        f"{existing.page_path}, cannot add from {path}"
                    )
                new_state.candidate_by_id[cid] = CandidateRef(
                    cid, keyword_id, path, item)
                status = str(item.get("status") or "")
                expires = parse_iso(item.get("lease_expires_at"))
                next_attempt = parse_iso(item.get("next_attempt_at"))
                if (
                    drain_visible
                    and (not next_attempt or next_attempt <= now_dt)
                    and (
                        status in {"pending", "ready", "failed_retryable"}
                        or status == "processing"
                        and (not expires or expires <= now_dt)
                    )
                    and bool(expected_profile_hash)
                    and _relevance_claimable(item, expected_profile_hash)
                ):
                    new_queue.append(cid)
                elif (
                    drain_visible
                    and next_attempt and next_attempt > now_dt
                    and status in {"pending", "ready", "failed_retryable"}
                    and bool(expected_profile_hash)
                    and _relevance_claimable(item, expected_profile_hash)
                ):
                    new_state.delayed_candidate_ids.add(cid)
                doi = _candidate_doi(item)
                if status == "emitted" and doi:
                    ref = EmittedPrimaryRef(cid, path, dict(item))
                    new_state.emitted_by_doi[doi] = select_stable_emitted_primary(
                        new_state.emitted_by_doi.get(doi), ref,
                    )
                if status in DURABLE_DOI_CANDIDATE_STATES and doi:
                    new_state.terminal_by_doi.setdefault(doi, []).append(cid)

            # ── Single atomic swap — readers see old or new, never mixed ─
            self._state = new_state
            self.dirty_pages.add(path)

    def pending_count(self, keyword_ids: Iterable[str] | None = None) -> int:
        with self._lock:
            self._promote_due_candidates()
            self.lookups += 1
            wanted = set(keyword_ids or self._state.claimable_by_keyword)
            return sum(
                1
                for keyword in wanted
                for cid in self._state.claimable_by_keyword.get(keyword, ())
                if _relevance_claimable(
                    self._state.candidate_by_id[cid].payload,
                    self.active_profile_hashes.get(keyword),
                )
            )

    def update_candidate(self, page_path: Path, item: Mapping[str, Any]) -> None:
        with self._lock:
            cid = str(item.get("candidate_id") or "")
            old = self._state.candidate_by_id.get(cid)
            keyword_id = old.keyword_id if old else str(
                (self._state.page_cache.get(page_path) or {}).get("keyword_id") or "")
            materialized = dict(item)

            # Copy-on-write clone.
            new_state = self._state.copy()
            new_state.candidate_by_id[cid] = CandidateRef(
                cid, keyword_id, page_path, materialized)
            self.dirty_pages.add(page_path)
            new_state.delayed_candidate_ids.discard(cid)
            for queue in new_state.claimable_by_keyword.values():
                try:
                    queue.remove(cid)
                except ValueError:
                    pass
            for doi, owner in list(new_state.processing_by_doi.items()):
                if owner == cid:
                    new_state.processing_by_doi.pop(doi, None)
            for doi, ref in list(new_state.emitted_by_doi.items()):
                if ref.candidate_id == cid:
                    new_state.emitted_by_doi.pop(doi, None)
            for doi, owners in list(new_state.terminal_by_doi.items()):
                if cid in owners:
                    new_state.terminal_by_doi[doi] = [
                        owner for owner in owners if owner != cid]
                    if not new_state.terminal_by_doi[doi]:
                        new_state.terminal_by_doi.pop(doi, None)
            doi = _candidate_doi(materialized)
            status = str(item.get("status") or "")
            expires = parse_iso(item.get("lease_expires_at"))
            next_attempt = parse_iso(item.get("next_attempt_at"))
            now_dt = datetime.now(timezone.utc)
            page = new_state.page_cache.get(page_path)
            expected_profile_hash = self.active_profile_hashes.get(keyword_id)
            if (
                status == "processing" and doi and expires and expires > now_dt
                and bool(expected_profile_hash)
                and _relevance_claimable(materialized, expected_profile_hash)
            ):
                new_state.processing_by_doi.setdefault(doi, cid)
            elif status == "emitted" and doi:
                ref = EmittedPrimaryRef(cid, page_path, materialized)
                new_state.emitted_by_doi[doi] = select_stable_emitted_primary(
                    new_state.emitted_by_doi.get(doi), ref,
                )
            if status in DURABLE_DOI_CANDIDATE_STATES and doi:
                new_state.terminal_by_doi.setdefault(doi, []).append(cid)
            if (
                page is not None and page_is_drain_visible(page)
                and (
                    status in {"pending", "ready", "failed_retryable"}
                    or status == "processing" and (not expires or expires <= now_dt)
                )
                and bool(expected_profile_hash)
                and _relevance_claimable(materialized, expected_profile_hash)
            ):
                if not next_attempt or next_attempt <= now_dt:
                    new_state.claimable_by_keyword.setdefault(
                        keyword_id, deque()).append(cid)
                else:
                    new_state.delayed_candidate_ids.add(cid)

            # Single atomic swap.
            self._state = new_state

    def _promote_due_candidates(self) -> None:
        now_dt = datetime.now(timezone.utc)
        for cid in tuple(self._state.delayed_candidate_ids):
            ref = self._state.candidate_by_id.get(cid)
            if ref is None:
                self._state.delayed_candidate_ids.discard(cid)
                continue
            next_attempt = parse_iso(ref.payload.get("next_attempt_at"))
            page = self._state.page_cache.get(ref.page_path)
            if (not next_attempt or next_attempt <= now_dt) and (
                page is not None and page_is_drain_visible(page)
            ):
                queue = self._state.claimable_by_keyword.setdefault(
                    ref.keyword_id, deque())
                expected_profile_hash = self.active_profile_hashes.get(ref.keyword_id)
                if (
                    expected_profile_hash
                    and _relevance_claimable(ref.payload, expected_profile_hash)
                    and cid not in queue
                ):
                    queue.append(cid)
                self._state.delayed_candidate_ids.discard(cid)

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


def validate_journal_drain_index(index: JournalDrainIndex) -> list[str]:
    """Audit every structural invariant of a :class:`JournalDrainIndex`.

    Returns a (possibly empty) list of human-readable violation descriptions.
    This is an *O(n)* scan over the index; call it after ``build()``, in
    tests, during explicit audit/debug, and before/after performance
    benchmarks — never on the hot drain path.
    """
    violations: list[str] = []
    with index._lock:
        by_id = index._state.candidate_by_id
        claimable = index._state.claimable_by_keyword
        processing = index._state.processing_by_doi
        emitted = index._state.emitted_by_doi
        terminal = index._state.terminal_by_doi
        cache = index._state.page_cache
        delayed = index._state.delayed_candidate_ids
        active = index.active_profile_hashes

        # 1. No cross-page candidate_id collision.
        cid_pages: dict[str, Path] = {}
        for cid, ref in by_id.items():
            prev = cid_pages.get(cid)
            if prev is not None and prev != ref.page_path:
                violations.append(
                    f"cross-page candidate_id collision: {cid} on {prev} and {ref.page_path}")
            cid_pages[cid] = ref.page_path

        # 2. Every queued / delayed / processing ref exists in by_id.
        for kw, queue_ids in claimable.items():
            for cid in queue_ids:
                if cid not in by_id:
                    violations.append(f"claimable queue {kw!r} refs missing candidate {cid}")
        for cid in delayed:
            if cid not in by_id:
                violations.append(f"delayed set refs missing candidate {cid}")
        for doi, cid in processing.items():
            if cid not in by_id:
                violations.append(f"processing_by_doi {doi} refs missing candidate {cid}")

        # 3. DOI-owner refs exist.
        for doi, ref in emitted.items():
            if ref.candidate_id not in by_id:
                violations.append(f"emitted_by_doi {doi} owner {ref.candidate_id} missing")
        for doi, cids in terminal.items():
            for cid in cids:
                if cid not in by_id:
                    violations.append(f"terminal_by_doi {doi} refs missing candidate {cid}")

        # 4. Emitted primary obeys stable-selection rule.
        for doi, ref in emitted.items():
            # Re-derive what the stable owner should be by scanning all
            # emitted candidates for this DOI across all pages.
            candidates_for_doi: list[tuple[str, EmittedPrimaryRef]] = []
            for cid, candidate_ref in by_id.items():
                if _candidate_doi(candidate_ref.payload) == doi:
                    status = str(candidate_ref.payload.get("status") or "")
                    if status == "emitted":
                        candidates_for_doi.append(
                            (cid, EmittedPrimaryRef(cid, candidate_ref.page_path, dict(candidate_ref.payload))))
            if candidates_for_doi:
                stable = candidates_for_doi[0][1]
                for _, cand in candidates_for_doi[1:]:
                    stable = select_stable_emitted_primary(stable, cand)
                if stable.candidate_id != ref.candidate_id:
                    violations.append(
                        f"emitted_by_doi {doi}: stored owner {ref.candidate_id} != "
                        f"stable owner {stable.candidate_id}")

        # 5. Page state ⇔ claimable consistency.
        for kw, queue_ids in claimable.items():
            expected_hash = active.get(kw)
            for cid in queue_ids:
                ref = by_id.get(cid)
                if ref is None:
                    continue
                page = cache.get(ref.page_path)
                if page is None:
                    violations.append(f"claimable {cid}: page {ref.page_path} not cached")
                    continue
                if not page_is_drain_visible(page):
                    violations.append(
                        f"claimable {cid}: page state {page.get('state')!r} is not drain-visible")
                if expected_hash and not _relevance_claimable(ref.payload, expected_hash):
                    violations.append(
                        f"claimable {cid}: relevance not passed for active profile hash")

        # 6. Active profile bindings present for every keyword with claimable.
        for kw in claimable:
            if kw not in active:
                violations.append(f"claimable keyword {kw!r} has no active profile binding")

    return violations


def _candidate_doi(item: Mapping[str, Any]) -> str:
    payload = item.get("candidate") if isinstance(item.get("candidate"), dict) else {}
    return normalize_doi(payload.get("doi") or "")


def _relevance_state(item: Mapping[str, Any]) -> str:
    """Return the orthogonal relevance state for one candidate.

    Journals written before relevance was introduced lack an explicit
    record and are treated as ``profile_unbound`` — they must be evaluated
    by a relevance finalizer before they become claimable.
    """
    relevance = item.get("relevance")
    if isinstance(relevance, Mapping):
        return str(relevance.get("state") or "profile_unbound")
    return "profile_unbound"


def _relevance_claimable(
    item: Mapping[str, Any], expected_profile_hash: str | None,
) -> bool:
    if _relevance_state(item) not in RELEVANCE_CLAIMABLE_STATES:
        return False
    if expected_profile_hash is None:
        return False  # fail closed — every call site must supply the active hash
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

    def _validate_page_path_identity(self, data: dict[str, Any], path: Path) -> None:
        """Reject pages whose filesystem path disagrees with their content identity.

        Uses the same canonical path builder as ``write_page()`` so the
        directory layout is defined in exactly one place.
        """
        resolved_root = self.root_dir.resolve()
        resolved_path = path.resolve()
        # 1. Resolved page must be inside resolved root.
        try:
            resolved_path.relative_to(resolved_root)
        except ValueError:
            raise JournalCorruptError(f"page outside journal root: {path}")
        # 2. No symlink or reparse point in components between root and leaf.
        #    The root itself may be a symlink/junction.
        current = path
        while True:
            try:
                current.relative_to(self.root_dir)
            except ValueError:
                break  # walked past root boundary
            if current == self.root_dir or current == self.root_dir.resolve():
                break
            if _path_is_reparse(current):
                raise JournalCorruptError(f"symlink/reparse in journal path: {current}")
            current = current.parent
        # 3. The path must match the canonical identity derived from content.
        expected = self.page_path(
            keyword_id=data["keyword_id"],
            query_id=data["query_id"],
            provider=data["provider"],
            lane=data["lane"],
            page_id=data["page_id"],
        )
        if resolved_path != expected.resolve():
            raise JournalCorruptError(
                f"journal path identity mismatch: {path} vs expected {expected}"
            )

    def read(self, path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise JournalCorruptError(f"journal JSON corrupt: {path}: {exc}") from exc
        result = _validate_page(data, path)
        self._validate_page_path_identity(result, path)
        return result

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
        lane_key: DiscoveryLaneKey,
        request_signature_value: dict[str, Any],
        request_cursor: str | None,
        next_cursor: str | None,
        provider_exhausted: bool,
        response_metadata: ProviderResponseMetadata,
        exhaustion_evidence: ExhaustionEvidence | None,
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
        if request_cursor is None:
            raise ValueError("durable provider pages require a concrete request_cursor")
        if lane_key.to_dict() != {
            "keyword_id": keyword_id,
            "query_id": query_id,
            "provider": provider,
            "mode": lane,
            "generation": int(generation),
            "request_signature": str(request_signature_value.get("hash") or ""),
        }:
            raise ValueError("lane_key does not match durable page identity")
        if provider_exhausted and exhaustion_evidence is None:
            raise ValueError("exhausted durable provider page requires exhaustion_evidence")
        if not provider_exhausted and exhaustion_evidence is not None:
            raise ValueError("non-exhausted durable provider page must not carry exhaustion_evidence")
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
            "lane_key": lane_key.to_dict(),
            "refresh_run_id": refresh_run_id,
            "page_sequence": page_sequence,
            "request_signature": request_signature_value,
            "request_cursor": request_cursor,
            "next_cursor": next_cursor,
            "provider_exhausted": bool(provider_exhausted),
            "returned_count": len(records),
            "response_metadata": response_metadata.to_dict(),
            "exhaustion_evidence": (
                None if exhaustion_evidence is None else exhaustion_evidence.to_dict()
            ),
            "state": state,
            "fetched_at": now,
            "cursor_committed_at": now if state == "cursor_committed" else None,
            "drained_at": None,
            "candidates": records,
            "statistics": _statistics(records),
        }

    def make_synthetic_page(self, **kwargs: Any) -> dict[str, Any]:
        """Build a complete v3 page for an isolated test fixture.

        Production code must call :meth:`make_page` with the real response
        metadata supplied by ``ProviderPageFetcher``.  Tests sometimes need a
        durable page without making a provider request; this explicit helper
        creates *synthetic but complete* evidence rather than reviving the
        removed v2/hash-only journal shape.  No runtime path calls it.
        """
        values = dict(kwargs)
        page_id = str(values["page_id"])
        keyword_id_value = str(values["keyword_id"])
        query_id_value = str(values["query_id"])
        provider = str(values["provider"])
        lane = str(values["lane"])
        generation = int(values.get("generation", 1))
        signature_value = values.get("request_signature_value")
        if signature_value is None:
            signature_value = request_signature(page_size=50)
        if not isinstance(signature_value, dict):
            raise TypeError("synthetic request_signature_value must be object")
        signature = RequestSignature.from_dict_strict(signature_value)
        values["request_signature_value"] = signature.to_dict()
        values.setdefault("generation", generation)
        # Older test fixtures commonly expressed the first request as None.
        # The synthetic helper turns that into the explicit durable sentinel;
        # production pages remain strict and never receive this normalization.
        if values.get("request_cursor") is None:
            values["request_cursor"] = INITIAL_CURSOR
        values.setdefault(
            "lane_key",
            DiscoveryLaneKey(
                keyword_id=keyword_id_value,
                query_id=query_id_value,
                provider=provider,  # type: ignore[arg-type]
                mode=lane,  # type: ignore[arg-type]
                generation=generation,
                request_signature=signature.hash,
            ),
        )
        candidates = values.get("candidates") or []
        metadata_value = values.get("response_metadata")
        if metadata_value is None:
            metadata = ProviderResponseMetadata(
                http_status=200,
                total_results=len(candidates),
                next_cursor_present=values.get("next_cursor") is not None,
                response_fingerprint=stable_hash(
                    "synthetic-provider-page",
                    page_id,
                    signature.hash,
                    values.get("request_cursor"),
                    values.get("next_cursor"),
                    len(candidates),
                    length=64,
                ),
                observed_at=now_iso(),
            )
        elif isinstance(metadata_value, ProviderResponseMetadata):
            metadata = metadata_value
        elif isinstance(metadata_value, Mapping):
            metadata = ProviderResponseMetadata.from_dict_strict(metadata_value)
        else:
            raise TypeError("synthetic response_metadata must be metadata object")
        values["response_metadata"] = metadata
        exhausted = bool(values.get("provider_exhausted", False))
        evidence_value = values.get("exhaustion_evidence")
        if exhausted and evidence_value is None:
            values["exhaustion_evidence"] = ExhaustionEvidence(
                provider=provider,
                query_id=query_id_value,
                request_signature=signature.hash,
                generation=generation,
                cursor_before=str(values["request_cursor"]),
                response_metadata=metadata,
                observed_at=metadata.observed_at,
            )
        elif isinstance(evidence_value, Mapping):
            values["exhaustion_evidence"] = ExhaustionEvidence.from_dict_strict(evidence_value)
        elif evidence_value is None:
            values["exhaustion_evidence"] = None
        return self.make_page(**values)

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
            # ── Universal relevance barrier: every candidate must carry an ──
            #     explicit, non-profile_unbound relevance record BEFORE the
            #     cursor advances.  This covers the all-terminal fast-path
            #     below as well as the normal cursor-commit path.
            _assert_relevance_finalized(data, path)
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

    def claim_candidate(
        self,
        page_path: Path,
        *,
        candidate_id_value: str,
        worker_id: str,
        lease_seconds: int,
        expected_profile_hash: str,
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
                if not _relevance_claimable(item, expected_profile_hash):
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
                if _assert_terminal_replay_equivalent(
                    item, new_status=new_status, updates=updates,
                ):
                    return dict(item)
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
        changed = False
        with self.lock_for(page_path):
            data = self.read(page_path)
            for item in data["candidates"]:
                result = by_id.get(str(item.get("candidate_id") or ""))
                if result is None:
                    continue
                new_status = str(result["new_status"])
                updates = result.get("updates")
                if _assert_terminal_replay_equivalent(
                    item,
                    new_status=new_status,
                    updates=updates if isinstance(updates, Mapping) else None,
                ):
                    committed.append(dict(item))
                    continue
                if item.get("status") == "processing" and item.get("claimed_by") != worker_id:
                    raise InvalidStateTransition("only claim owner may commit processing result")
                _transition_candidate(item, new_status)  # type: ignore[arg-type]
                if isinstance(updates, Mapping):
                    item.update(updates)
                if new_status in TERMINAL_CANDIDATE_STATES or new_status == "failed_retryable":
                    item["claimed_by"] = None
                    item["claimed_at"] = None
                    item["lease_expires_at"] = None
                committed.append(dict(item))
                changed = True
            if len(committed) != len(by_id):
                missing = sorted(set(by_id) - {str(item["candidate_id"]) for item in committed})
                raise KeyError(f"candidates not found: {','.join(missing)}")
            if changed:
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


def _assert_terminal_replay_equivalent(
    item: Mapping[str, Any],
    *,
    new_status: str,
    updates: Mapping[str, Any] | None,
) -> bool:
    """Allow terminal replay only when it is a byte-preserving no-op."""
    old_status = str(item.get("status") or "")
    if old_status not in TERMINAL_CANDIDATE_STATES:
        return False
    if new_status != old_status:
        raise InvalidStateTransition(
            f"terminal candidate replay cannot change status {old_status} -> {new_status}"
        )
    mismatched = sorted(
        key
        for key, value in (updates or {}).items()
        if key not in item or item[key] != value
    )
    if mismatched:
        raise InvalidStateTransition(
            "terminal candidate replay cannot overwrite fields: "
            + ",".join(mismatched)
        )
    return True


def _transition_candidate(item: dict[str, Any], new_state: CandidateState) -> None:
    old = item.get("status")
    if new_state not in _CANDIDATE_TRANSITIONS.get(old, set()):
        if old == new_state:
            return
        raise InvalidStateTransition(f"candidate {old} -> {new_state} is not allowed")
    item["status"] = new_state


def _assert_relevance_finalized(data: dict[str, Any], path: Path) -> None:
    """Every candidate must carry an explicit, non-profile_unbound relevance record.

    Called before ``mark_cursor_committed`` transitions the page state.
    This covers both the normal path and the all-terminal fast-path to
    ``drained``.  ``profile_unbound`` or a missing relevance record are
    always rejected: a new page must be evaluated before its cursor can
    advance.
    """
    allowed = RELEVANCE_STATES - {RelevanceState.PROFILE_UNBOUND}
    for item in data.get("candidates", []):
        relevance = item.get("relevance")
        if not isinstance(relevance, Mapping):
            raise InvalidStateTransition(
                f"candidate {item.get('candidate_id')!r} is missing a "
                f"relevance record and cannot be cursor-committed: {path}"
            )
        state = str(relevance.get("state") or "")
        if state == RelevanceState.PROFILE_UNBOUND:
            raise InvalidStateTransition(
                f"candidate {item.get('candidate_id')!r} is still "
                f"profile_unbound and cannot be cursor-committed: {path}"
            )
        if state not in allowed:
            raise InvalidStateTransition(
                f"candidate {item.get('candidate_id')!r} has unknown "
                f"relevance state {state!r}: {path}"
            )


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
