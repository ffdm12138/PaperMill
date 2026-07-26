"""Discovery v4 strict page journal contract.

Canonical ``ProviderPageJournalV4`` frozen dataclass — the ONLY page journal
format accepted in production — plus the single authority for v4 page
journal validation and helpers: page/candidate state machines, identity
hashes, claim/reference records, the drain-index audit, and the
profile-closure page transformation.  The validator and helper logic
previously lived in ``src/discovery/page_journal.py``; that retired alias
shell is deleted and this contract module is the sole implementation.  All
fields are required by ``PAGE_V4_FIELDS`` and validated at construction time.

This module is data + pure validation only.  Mutation-applying, protocol,
and filesystem helpers live in ``src.discovery.stores.page_journal_ops``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Mapping

from src.discovery.constants import INITIAL_CURSOR
from src.discovery.contracts.enums import JournalStateV4
from src.discovery.contracts.notebook import (
    PROVIDERS,
    detect_query_language,
    keyword_id as make_keyword_id,
    normalize_keyword,
    query_identity,
)
from src.discovery.models import PaperCandidate
from src.utils.identifiers import normalize_doi, normalize_title
from src.utils.timestamps import utc_now_iso as now_iso
from src.discovery.relevance import (
    RELEVANCE_REASON_VALUES,
    RELEVANCE_STATES,
    RelevanceReason,
    RelevanceState,
)

# ── Schema version ───────────────────────────────────────────────────────

PAGE_SCHEMA_VERSION_V4 = "4.0"

# ── Exact field set ──────────────────────────────────────────────────────

PAGE_V4_FIELDS: frozenset[str] = frozenset({
    "schema_version", "page_id", "keyword_id", "keyword_zh",
    "query_id", "query", "query_language", "provider", "lane",
    "generation", "lane_key", "request_signature",
    "request_cursor", "next_cursor",
    "provider_exhausted", "returned_count",
    "response_metadata", "exhaustion_evidence", "state",
    "fetched_at", "cursor_committed_at", "drained_at",
    "candidates", "statistics",
    "refresh_run_id", "page_sequence", "checksum",
})


# ── Validation helpers ───────────────────────────────────────────────────

def _check_type(value: Any, expected: type, field_name: str) -> None:
    """Validate exact type — rejects bool for int fields."""
    if type(value) is not expected:
        if expected is int and isinstance(value, bool):
            raise TypeError(
                f"{field_name} must be int, got bool ({value!r})"
            )
        raise TypeError(
            f"{field_name} must be {expected.__name__}, "
            f"got {type(value).__name__} ({value!r})"
        )


def _check_non_blank(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-blank str, got {value!r}")


def compute_checksum(data: dict[str, Any]) -> str:
    """Canonical checksum over the serialized payload (excluding checksum field)."""
    payload = {k: v for k, v in sorted(data.items()) if k != "checksum"}
    raw = _canonical_json_bytes(payload)
    return hashlib.sha256(raw).hexdigest()


def _canonical_json_bytes(obj: Any) -> bytes:
    # Persisted checksum encoding uses indent=2 — do NOT swap to
    # src.utils.canonical_json (compact separators would change stored hashes).
    import json
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


# ── Provider page journal v4 ─────────────────────────────────────────────


@dataclass(frozen=True)
class ProviderPageJournalV4:
    """One durable provider page in the v4 workspace journal.

    Every field required by ``PAGE_V4_FIELDS`` is present.  Construction is
    the only path — use ``from_dict_strict()`` to round-trip from disk.
    """

    schema_version: str = PAGE_SCHEMA_VERSION_V4
    page_id: str = ""
    keyword_id: str = ""
    keyword_zh: str = ""
    query_id: str = ""
    query: str = ""
    query_language: str = ""
    provider: str = ""
    lane: str = ""
    generation: int = 1
    lane_key: dict[str, Any] | None = None
    request_signature: dict[str, Any] | None = None
    request_cursor: str = INITIAL_CURSOR
    next_cursor: str | None = None
    provider_exhausted: bool = False
    returned_count: int = 0
    response_metadata: dict[str, Any] | None = None
    exhaustion_evidence: dict[str, Any] | None = None
    state: str = "fetched"
    fetched_at: str = ""
    cursor_committed_at: str | None = None
    drained_at: str | None = None
    candidates: tuple[Any, ...] = ()
    statistics: dict[str, Any] = field(default_factory=dict)
    refresh_run_id: str | None = None
    page_sequence: int = 0
    checksum: str = ""

    def __post_init__(self) -> None:
        if type(self.schema_version) is not str or self.schema_version != PAGE_SCHEMA_VERSION_V4:
            raise ValueError(
                f"schema_version must be {PAGE_SCHEMA_VERSION_V4!r}, "
                f"got {self.schema_version!r}"
            )

        # returned_count must be int (not bool) and match len(candidates)
        _check_type(self.returned_count, int, "returned_count")
        if self.returned_count < 0:
            raise ValueError(f"returned_count must be >= 0, got {self.returned_count}")

        actual_candidates = len(self.candidates)
        if self.returned_count != actual_candidates:
            raise ValueError(
                f"returned_count ({self.returned_count}) must equal "
                f"len(candidates) ({actual_candidates})"
            )

        # provider_exhausted must be bool
        _check_type(self.provider_exhausted, bool, "provider_exhausted")

        if self.provider_exhausted:
            if self.next_cursor is not None:
                raise ValueError("exhausted page must have next_cursor=None")
            if self.exhaustion_evidence is None:
                raise ValueError("exhausted page requires exhaustion_evidence")
        else:
            if self.exhaustion_evidence is not None:
                raise ValueError(
                    "non-exhausted page must not have exhaustion_evidence"
                )

        # generation must be int >= 1
        _check_type(self.generation, int, "generation")
        if self.generation < 1:
            raise ValueError(f"generation must be >= 1, got {self.generation}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "page_id": self.page_id,
            "keyword_id": self.keyword_id,
            "keyword_zh": self.keyword_zh,
            "query_id": self.query_id,
            "query": self.query,
            "query_language": self.query_language,
            "provider": self.provider,
            "lane": self.lane,
            "generation": self.generation,
            "lane_key": self.lane_key,
            "request_signature": self.request_signature,
            "request_cursor": self.request_cursor,
            "next_cursor": self.next_cursor,
            "provider_exhausted": self.provider_exhausted,
            "returned_count": self.returned_count,
            "response_metadata": self.response_metadata,
            "exhaustion_evidence": self.exhaustion_evidence,
            "state": self.state,
            "fetched_at": self.fetched_at,
            "cursor_committed_at": self.cursor_committed_at,
            "drained_at": self.drained_at,
            "candidates": list(self.candidates),
            "statistics": dict(self.statistics),
            "refresh_run_id": self.refresh_run_id,
            "page_sequence": self.page_sequence,
            "checksum": self.checksum,
        }

    @classmethod
    def from_dict_strict(cls, data: Mapping[str, Any]) -> "ProviderPageJournalV4":
        """Construct from a dict, rejecting unknown/missing fields and coercions."""
        if not isinstance(data, (dict, Mapping)):
            raise TypeError(f"expected dict, got {type(data).__name__}")

        extra = set(data) - PAGE_V4_FIELDS
        if extra:
            raise ValueError(f"ProviderPageJournalV4 unknown fields: {sorted(extra)}")
        missing = PAGE_V4_FIELDS - set(data)
        if missing:
            raise ValueError(f"ProviderPageJournalV4 missing fields: {sorted(missing)}")

        sv = data["schema_version"]
        if type(sv) is not str or sv != PAGE_SCHEMA_VERSION_V4:
            raise ValueError(f"schema_version must be {PAGE_SCHEMA_VERSION_V4!r}")

        rc = data["returned_count"]
        if type(rc) is not int or isinstance(rc, bool):
            raise TypeError(f"returned_count must be int, got {type(rc).__name__}")
        if rc < 0:
            raise ValueError(f"returned_count must be >= 0")

        pe = data["provider_exhausted"]
        if type(pe) is not bool:
            raise TypeError(f"provider_exhausted must be bool")

        gen = data["generation"]
        if type(gen) is not int or isinstance(gen, bool):
            raise TypeError(f"generation must be int, got {type(gen).__name__}")

        # Compute checksum from canonical form
        expected_checksum = compute_checksum(dict(data))
        stored_checksum = data.get("checksum", "")
        if stored_checksum and stored_checksum != expected_checksum:
            raise ValueError(
                f"checksum mismatch: stored={stored_checksum[:16]}..., "
                f"computed={expected_checksum[:16]}..."
            )

        return cls(
            schema_version=PAGE_SCHEMA_VERSION_V4,
            page_id=str(data["page_id"]),
            keyword_id=str(data["keyword_id"]),
            keyword_zh=str(data["keyword_zh"]),
            query_id=str(data["query_id"]),
            query=str(data["query"]),
            query_language=str(data["query_language"]),
            provider=str(data["provider"]),
            lane=str(data["lane"]),
            generation=gen,
            lane_key=dict(data["lane_key"]) if isinstance(data.get("lane_key"), dict) else None,
            request_signature=dict(data["request_signature"]) if isinstance(data.get("request_signature"), dict) else None,
            request_cursor=str(data["request_cursor"]),
            next_cursor=data.get("next_cursor"),
            provider_exhausted=pe,
            returned_count=rc,
            response_metadata=dict(data["response_metadata"]) if isinstance(data.get("response_metadata"), dict) else None,
            exhaustion_evidence=dict(data["exhaustion_evidence"]) if isinstance(data.get("exhaustion_evidence"), dict) else None,
            state=str(data["state"]),
            fetched_at=str(data["fetched_at"]),
            cursor_committed_at=data.get("cursor_committed_at"),
            drained_at=data.get("drained_at"),
            candidates=tuple(data.get("candidates", [])),
            statistics=dict(data.get("statistics", {})),
            refresh_run_id=data.get("refresh_run_id"),
            page_sequence=int(data.get("page_sequence", 0)) if not isinstance(data.get("page_sequence"), bool) else 0,
            checksum=expected_checksum,
        )


# ── Non-v4 state detection ───────────────────────────────────────────────


class UnexpectedNonV4StateError(RuntimeError):
    """Raised when production code encounters a non-v4 file in the active
    workspace.  This is a hard failure — v4 production code never reads
    v2/v3 files.
    """


# Single canonical schema version is PAGE_SCHEMA_VERSION_V4 (above).
# Local alias kept only so existing call-sites compile without churn.
PAGE_SCHEMA_VERSION = PAGE_SCHEMA_VERSION_V4

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
        and relevance_state(candidate) in RELEVANCE_PROFILE_CHANGE_CLOSEABLE_STATES
        and old_hash != target_profile_hash
    )

PAGE_TRANSITIONS = {
    "fetched": {"cursor_committed", "failed"},
    "cursor_committed": {"draining", "drained"},
    "draining": {"cursor_committed", "drained"},
    "drained": set(),
    "failed": set(),
}

CANDIDATE_TRANSITIONS = {
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


def make_candidate_record(
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


def validate_page(data: Any, path: Path | None = None) -> dict[str, Any]:
    """Strictly validate one active complete schema-v4 provider-page journal."""
    from src.discovery.contracts.lane_history import (
        ExhaustionEvidence,
        ProviderResponseMetadata,
    )
    from src.discovery.execution.lane_models import (
        DiscoveryLaneKey,
        RequestSignature,
    )
    if not isinstance(data, dict):
        raise JournalCorruptError(f"journal root is not object: {path or ''}")
    missing = sorted(PAGE_V4_FIELDS - set(data))
    if missing:
        raise JournalCorruptError(f"journal missing keys {missing}: {path or ''}")
    unexpected = sorted(set(data) - PAGE_V4_FIELDS)
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
    if language not in {"zh", "en", "mixed"} or detect_query_language(query) != language:
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
    if data.get("state") not in PAGE_TRANSITIONS:
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
        if status not in CANDIDATE_TRANSITIONS:
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
    # Checksum is the final integrity gate — after all field-level checks pass.
    stored_checksum = data.get("checksum", "")
    if stored_checksum:
        expected = compute_checksum(data)
        if stored_checksum != expected:
            raise JournalCorruptError(
                f"journal checksum mismatch: stored={stored_checksum[:16]}..., "
                f"computed={expected[:16]}...: {path or ''}"
            )
    return data


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
                if candidate_doi(candidate_ref.payload) == doi:
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
                if expected_hash and not relevance_claimable(ref.payload, expected_hash):
                    violations.append(
                        f"claimable {cid}: relevance not passed for active profile hash")

        # 6. Active profile bindings present for every keyword with claimable.
        for kw in claimable:
            if kw not in active:
                violations.append(f"claimable keyword {kw!r} has no active profile binding")

    return violations


def candidate_doi(item: Mapping[str, Any]) -> str:
    payload = item.get("candidate") if isinstance(item.get("candidate"), dict) else {}
    return normalize_doi(payload.get("doi") or "")


def relevance_state(item: Mapping[str, Any]) -> str:
    """Return the orthogonal relevance state for one candidate.

    Journals written before relevance was introduced lack an explicit
    record and are treated as ``profile_unbound`` — they must be evaluated
    by a relevance finalizer before they become claimable.
    """
    relevance = item.get("relevance")
    if isinstance(relevance, Mapping):
        return str(relevance.get("state") or "profile_unbound")
    return "profile_unbound"


def relevance_claimable(
    item: Mapping[str, Any], expected_profile_hash: str | None,
) -> bool:
    if relevance_state(item) not in RELEVANCE_CLAIMABLE_STATES:
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
    if all_terminal(data["candidates"]) and data["state"] in {
        "cursor_committed", "draining",
    }:
        data["state"] = "drained"
        data["drained_at"] = data.get("drained_at") or closure_timestamp
    data["statistics"] = compute_statistics(data["candidates"])
    data["checksum"] = compute_checksum(data)
    validate_page(data)
    return _serialized_page_bytes(data)


def all_terminal(candidates: list[dict[str, Any]]) -> bool:
    return all(
        item.get("status") in TERMINAL_CANDIDATE_STATES
        or relevance_state(item) in RELEVANCE_TERMINAL_STATES
        for item in candidates
    )


def compute_statistics(candidates: list[dict[str, Any]]) -> dict[str, int]:
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
        relevance_state_value = relevance_state(item)
        relevance_key = f"relevance_{relevance_state_value}"
        if relevance_key in stats:
            stats[relevance_key] += 1
        if status in TERMINAL_CANDIDATE_STATES or relevance_state_value in RELEVANCE_TERMINAL_STATES:
            stats["terminal"] += 1
        else:
            if relevance_state_value == "passed":
                stats["pending"] += 1
        if status in stats:
            stats[status] += 1
        if status == "invalid_doi":
            stats["invalid"] += 1
    return stats
