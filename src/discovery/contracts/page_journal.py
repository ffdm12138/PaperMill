"""Discovery v4 strict page journal contract.

Canonical ``ProviderPageJournalV4`` frozen dataclass — the ONLY page journal
format accepted in production.  All fields are required by ``PAGE_V4_FIELDS``
and validated at construction time.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping

from src.discovery.constants import INITIAL_CURSOR
from src.discovery.contracts.enums import JournalStateV4

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


def _compute_checksum(data: dict[str, Any]) -> str:
    """Canonical checksum over the serialized payload (excluding checksum field)."""
    payload = {k: v for k, v in sorted(data.items()) if k != "checksum"}
    raw = _canonical_json_bytes(payload)
    return hashlib.sha256(raw).hexdigest()


def _canonical_json_bytes(obj: Any) -> bytes:
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
        expected_checksum = _compute_checksum(dict(data))
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
