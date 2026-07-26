"""Physical lane models for DOI discovery.

This module is the single source of truth for:

- :class:`DiscoveryLaneKey` — unique identity of one physical lane
  (keyword, query, provider, mode, generation, request_signature).
- :class:`LaneState` — lane terminal state (frozen enum).
- :class:`StopReason` — why the lane stopped (frozen enum).
- :class:`LaneCounters` — structured counters (pages, requests, items).
- :class:`LaneError` — typed lane error.
- :class:`LaneOutcome` — complete lane result, the **only** input to
  ReportBuilder.
- :class:`LaneExecutionSpec` — full execution specification for one lane.
- :class:`RequestSignature` — typed request signature model.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Literal, Mapping


class LaneState(str, Enum):
    """Production lane state (single source of truth — matches the
    LaneMachine transition table).

    No other module defines a parallel ``LaneStatus`` set.
    """
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    EXHAUSTED = "exhausted"
    BUDGET_STOPPED = "budget_stopped"
    RETRYABLE_FAILED = "retryable_failed"
    PERMANENT_FAILED = "permanent_failed"
    REPAIR_REQUIRED = "repair_required"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"


class StopReason(str, Enum):
    """Frozen stop-reason vocabulary — every lane terminal event maps to
    exactly one entry.  No other module defines a parallel set."""
    REFRESH_WINDOW_COMPLETE = "refresh_window_complete"
    BACKFILL_PAGE_COMPLETE = "backfill_page_complete"
    PROVIDER_EXHAUSTED = "provider_exhausted"
    LANE_PAGE_BUDGET_REACHED = "lane_page_budget_reached"
    BATCH_PAGE_BUDGET_REACHED = "batch_page_budget_reached"
    PROVIDER_REQUEST_BUDGET_REACHED = "provider_request_budget_reached"
    CANDIDATE_BACKPRESSURE = "candidate_backpressure"
    RETRY_EXHAUSTED = "retry_exhausted"
    STATE_LOCK_TIMEOUT = "state_lock_timeout"
    CIRCUIT_OPEN = "circuit_open"
    PERMANENT_PROVIDER_ERROR = "permanent_provider_error"
    CURSOR_CONFLICT = "cursor_conflict"
    JOURNAL_CORRUPTION = "journal_corruption"
    LOCAL_CONSISTENCY_ERROR = "local_consistency_error"
    USER_INTERRUPTED = "user_interrupted"
    SKIPPED_BY_MODE = "skipped_by_mode"



@dataclass(frozen=True, order=True)
class DiscoveryLaneKey:
    """Unique, ordered identity for one physical discovery lane.

    A physical lane is exactly ``(keyword, query, provider, mode)`` at a
    specific ``(generation, request_signature)``.  Serialization is stable
    (``repr`` / ``str`` produce reliable monotonic keys).
    """
    keyword_id: str
    query_id: str
    provider: Literal["openalex", "crossref"]
    mode: Literal["refresh", "backfill"]
    generation: int = 0
    request_signature: str = ""

    def __post_init__(self) -> None:
        if not self.keyword_id or not isinstance(self.keyword_id, str):
            raise ValueError(f"keyword_id must be non-empty str, got {self.keyword_id!r}")
        if not self.query_id or not isinstance(self.query_id, str):
            raise ValueError(f"query_id must be non-empty str, got {self.query_id!r}")
        if self.provider not in ("openalex", "crossref"):
            raise ValueError(f"provider must be openalex or crossref, got {self.provider!r}")
        if self.mode not in ("refresh", "backfill"):
            raise ValueError(f"mode must be refresh or backfill, got {self.mode!r}")
        if not isinstance(self.generation, int) or isinstance(self.generation, bool) or self.generation < 0:
            raise ValueError(f"generation must be non-negative int (not bool), got {self.generation!r}")
        if not isinstance(self.request_signature, str) or not self.request_signature:
            raise ValueError(f"request_signature must be non-empty str, got {self.request_signature!r}")

    def __str__(self) -> str:
        return (f"{self.keyword_id}:{self.query_id}:{self.provider}:"
                f"{self.mode}:g{self.generation}:{self.request_signature[:8]}")

    def stable_id(self) -> str:
        """Stable, sortable lane identity for budget tracking and logging."""
        return str(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "keyword_id": self.keyword_id,
            "query_id": self.query_id,
            "provider": self.provider,
            "mode": self.mode,
            "generation": self.generation,
            "request_signature": self.request_signature,
        }

    @staticmethod
    def from_dict_strict(d: dict[str, object]) -> "DiscoveryLaneKey":
        allowed = {"keyword_id", "query_id", "provider", "mode", "generation", "request_signature"}
        extra = set(d) - allowed
        if extra:
            raise ValueError(f"DiscoveryLaneKey unknown fields: {sorted(extra)}")
        missing = allowed - set(d)
        if missing:
            raise ValueError(f"DiscoveryLaneKey missing fields: {sorted(missing)}")

        # Strict type checks — no coercion.  bool rejects for generation
        # because isinstance(True, int) is True, so we guard explicitly.
        for field_name in ("keyword_id", "query_id", "request_signature"):
            if not isinstance(d[field_name], str):
                raise TypeError(
                    f"DiscoveryLaneKey.{field_name} must be str, "
                    f"got {type(d[field_name]).__name__}"
                )

        gen = d["generation"]
        if isinstance(gen, bool) or not isinstance(gen, int):
            raise TypeError(
                f"DiscoveryLaneKey.generation must be int, "
                f"got {type(gen).__name__}"
            )
        if gen < 0:
            raise ValueError(
                f"DiscoveryLaneKey.generation must be >= 0, got {gen}"
            )

        provider = d["provider"]
        if not isinstance(provider, str) or provider not in ("openalex", "crossref"):
            raise TypeError(
                f"DiscoveryLaneKey.provider must be 'openalex' or 'crossref', "
                f"got {provider!r}"
            )

        mode = d["mode"]
        if not isinstance(mode, str) or mode not in ("refresh", "backfill"):
            raise TypeError(
                f"DiscoveryLaneKey.mode must be 'refresh' or 'backfill', "
                f"got {mode!r}"
            )

        # Validate non-empty identity fields (value checks, post-type-check)
        if not str(d["keyword_id"]).strip():
            raise ValueError("DiscoveryLaneKey.keyword_id must be non-blank")
        if not str(d["query_id"]).strip():
            raise ValueError("DiscoveryLaneKey.query_id must be non-blank")

        return DiscoveryLaneKey(
            keyword_id=d["keyword_id"],  # type: ignore[arg-type]
            query_id=d["query_id"],  # type: ignore[arg-type]
            provider=provider,  # type: ignore[arg-type]
            mode=mode,  # type: ignore[arg-type]
            generation=gen,  # type: ignore[arg-type]
            request_signature=d["request_signature"],
        )

    def with_generation(self, gen: int, sig: str) -> "DiscoveryLaneKey":
        return DiscoveryLaneKey(
            keyword_id=self.keyword_id, query_id=self.query_id,
            provider=self.provider, mode=self.mode,
            generation=gen, request_signature=sig,
        )


@dataclass
class LaneCounters:
    """Structured counters collected during one physical lane execution.

    These are the ONLY counters the ReportBuilder reads.  No other module
    defines a parallel counter set.
    """
    # -- logical page counters --
    logical_pages_attempted: int = 0
    pages_fetched: int = 0
    pages_recovered: int = 0
    pages_durable: int = 0
    pages_cursor_committed: int = 0
    candidates_observed: int = 0
    candidates_processed: int = 0

    # -- real HTTP attempt counters --
    provider_requests_attempted: int = 0
    provider_requests_retried: int = 0
    provider_requests_succeeded: int = 0
    provider_requests_failed: int = 0

    # -- items --
    items_returned: int = 0

    # -- local errors (not provider failures) --
    local_retryable_failures: int = 0
    local_consistency_failures: int = 0
    cursor_conflicts: int = 0


@dataclass(frozen=True)
class LaneError:
    """A single lane error — no full stack traces, no credentials."""
    category: str  # e.g. "provider_retryable", "cursor_conflict"
    message: str   # safe message (< 500 chars)
    count: int = 1


@dataclass(frozen=True)
class LaneOutcome:
    """Complete result of one physical lane execution.

    This is the **only** object that flows into the ReportBuilder.
    The coordinator never derives keyword/batch status by heuristic;
    it feeds the list of ``LaneOutcome`` s into the builder.
    """
    key: DiscoveryLaneKey
    state: LaneState
    stop_reason: StopReason
    counters: LaneCounters
    exhaustion_evidence: "ExhaustionEvidence | None"
    errors: tuple[LaneError, ...] = ()

    def __post_init__(self) -> None:
        if self.state == LaneState.EXHAUSTED and self.exhaustion_evidence is None:
            raise ValueError(
                "LaneOutcome with state=EXHAUSTED must carry non-None exhaustion_evidence"
            )
        if self.state != LaneState.EXHAUSTED and self.exhaustion_evidence is not None:
            raise ValueError(
                f"LaneOutcome with state={self.state.value} must not carry exhaustion_evidence"
            )

    @property
    def durable_progress(self) -> bool:
        """Unified durable-progress predicate.

        ``items_returned > 0`` is NOT required (an empty durable page with a
        committed cursor has made progress).
        """
        c = self.counters
        return bool(
            c.pages_durable > 0
            or c.pages_cursor_committed > 0
            or c.candidates_processed > 0
        )


@dataclass(frozen=True)
class ExhaustionEvidence:
    """Typed exhaustion evidence — never a raw ``dict[str, Any]``."""
    provider: str
    query_id: str
    request_signature: str
    generation: int
    cursor_before: str
    response_metadata: "ProviderResponseMetadata"
    observed_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "query_id": self.query_id,
            "request_signature": self.request_signature,
            "generation": self.generation,
            "cursor_before": self.cursor_before,
            "response_metadata": self.response_metadata.to_dict(),
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict_strict(cls, value: Mapping[str, object]) -> "ExhaustionEvidence":
        required = {
            "provider", "query_id", "request_signature", "generation",
            "cursor_before", "response_metadata", "observed_at",
        }
        if set(value) != required:
            raise ValueError("ExhaustionEvidence has an invalid field set")
        if value["provider"] not in {"openalex", "crossref"}:
            raise ValueError("ExhaustionEvidence.provider is invalid")
        if not isinstance(value["query_id"], str) or not value["query_id"]:
            raise ValueError("ExhaustionEvidence.query_id must be non-blank")
        if not isinstance(value["request_signature"], str) or not value["request_signature"]:
            raise ValueError("ExhaustionEvidence.request_signature must be non-blank")
        if isinstance(value["generation"], bool) or not isinstance(value["generation"], int):
            raise TypeError("ExhaustionEvidence.generation must be int")
        if not isinstance(value["cursor_before"], str):
            raise TypeError("ExhaustionEvidence.cursor_before must be str")
        if not isinstance(value["response_metadata"], Mapping):
            raise TypeError("ExhaustionEvidence.response_metadata must be object")
        if not isinstance(value["observed_at"], str) or not value["observed_at"]:
            raise ValueError("ExhaustionEvidence.observed_at must be non-blank")
        return cls(
            provider=value["provider"],  # type: ignore[arg-type]
            query_id=value["query_id"],  # type: ignore[arg-type]
            request_signature=value["request_signature"],  # type: ignore[arg-type]
            generation=value["generation"],  # type: ignore[arg-type]
            cursor_before=value["cursor_before"],  # type: ignore[arg-type]
            response_metadata=ProviderResponseMetadata.from_dict_strict(value["response_metadata"]),
            observed_at=value["observed_at"],  # type: ignore[arg-type]
        )


# ── DurableProviderPage (Phase 5) ──────────────────────────────────────

@dataclass(frozen=True)
class ProviderResponseMetadata:
    """Real provider response metadata persisted in the page journal.

    NOT persisted: Authorization headers, API keys, or full URL query strings.
    """
    http_status: int
    provider_request_id: str | None = None
    retry_after_observed: float | None = None
    total_results: int | None = None
    next_cursor_present: bool = False
    response_fingerprint: str = ""
    observed_at: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.http_status, bool) or not isinstance(self.http_status, int) or not 100 <= self.http_status <= 599:
            raise ValueError("ProviderResponseMetadata.http_status must be a real HTTP status")
        if self.provider_request_id is not None and not isinstance(self.provider_request_id, str):
            raise TypeError("ProviderResponseMetadata.provider_request_id must be str or None")
        if self.retry_after_observed is not None and (
            isinstance(self.retry_after_observed, bool) or not isinstance(self.retry_after_observed, (int, float))
            or self.retry_after_observed < 0
        ):
            raise TypeError("ProviderResponseMetadata.retry_after_observed must be non-negative numeric or None")
        if self.total_results is not None and (
            isinstance(self.total_results, bool) or not isinstance(self.total_results, int)
            or self.total_results < 0
        ):
            raise TypeError("ProviderResponseMetadata.total_results must be non-negative int or None")
        if not isinstance(self.next_cursor_present, bool):
            raise TypeError("ProviderResponseMetadata.next_cursor_present must be bool")
        if not isinstance(self.response_fingerprint, str) or not self.response_fingerprint:
            raise ValueError("ProviderResponseMetadata.response_fingerprint must be non-blank")
        if not isinstance(self.observed_at, str) or not self.observed_at:
            raise ValueError("ProviderResponseMetadata.observed_at must be non-blank")

    def to_dict(self) -> dict[str, object]:
        return {
            "http_status": self.http_status,
            "provider_request_id": self.provider_request_id,
            "retry_after_observed": self.retry_after_observed,
            "total_results": self.total_results,
            "next_cursor_present": self.next_cursor_present,
            "response_fingerprint": self.response_fingerprint,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict_strict(cls, value: Mapping[str, object]) -> "ProviderResponseMetadata":
        required = {
            "http_status", "provider_request_id", "retry_after_observed",
            "total_results", "next_cursor_present", "response_fingerprint", "observed_at",
        }
        if set(value) != required:
            raise ValueError("ProviderResponseMetadata has an invalid field set")
        return cls(
            http_status=value["http_status"],  # type: ignore[arg-type]
            provider_request_id=value["provider_request_id"],  # type: ignore[arg-type]
            retry_after_observed=value["retry_after_observed"],  # type: ignore[arg-type]
            total_results=value["total_results"],  # type: ignore[arg-type]
            next_cursor_present=value["next_cursor_present"],  # type: ignore[arg-type]
            response_fingerprint=value["response_fingerprint"],  # type: ignore[arg-type]
            observed_at=value["observed_at"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_request_outcome(
        cls,
        outcome: Any,
        *,
        fingerprint: str = "",
        observed_at: str = "",
    ) -> "ProviderResponseMetadata":
        """Build from a successful ``RequestOutcome`` without fake defaults."""
        status = getattr(outcome, "status_code", None)
        if isinstance(status, bool) or not isinstance(status, int):
            raise ValueError("RequestOutcome lacks a real HTTP status")
        rid = getattr(outcome, "provider_request_id", None)
        retry_after = getattr(outcome, "retry_after_observed", None)
        total = getattr(outcome, "total_results", None)
        has_next = bool(getattr(outcome, "next_cursor", None))
        fp = fingerprint or str(getattr(outcome, "response_fingerprint", ""))
        ts = observed_at or str(getattr(outcome, "observed_at", ""))
        return cls(
            http_status=status,
            provider_request_id=str(rid) if rid is not None else None,
            retry_after_observed=float(retry_after) if retry_after is not None else None,
            total_results=int(total) if total is not None else None,
            next_cursor_present=has_next,
            response_fingerprint=fp,
            observed_at=ts,
        )


@dataclass(frozen=True)
class DurableProviderPage:
    """The typed, durable representation of one fetched journal page.

    Replaces the ad-hoc ``SimpleNamespace`` proxy used in the recovery path.
    """
    page_id: str
    lane_key: DiscoveryLaneKey
    cursor_before: str
    next_cursor: str | None
    returned_count: int
    provider_exhausted: bool
    exhaustion_evidence: ExhaustionEvidence | None
    response_metadata: ProviderResponseMetadata
    journal_state: str = "fetched"
    candidates: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.page_id, str) or not self.page_id:
            raise ValueError("DurableProviderPage.page_id must be non-blank")
        if not isinstance(self.cursor_before, str):
            raise TypeError("DurableProviderPage.cursor_before must be str")
        if isinstance(self.returned_count, bool) or not isinstance(self.returned_count, int) or self.returned_count < 0:
            raise ValueError("DurableProviderPage.returned_count must be non-negative int")
        if self.returned_count != len(self.candidates):
            raise ValueError("DurableProviderPage.returned_count must match candidates")
        if self.provider_exhausted != (self.exhaustion_evidence is not None):
            raise ValueError("DurableProviderPage exhaustion evidence does not match exhaustion state")
        evidence = self.exhaustion_evidence
        if evidence is not None and (
            evidence.provider != self.lane_key.provider
            or evidence.query_id != self.lane_key.query_id
            or evidence.request_signature != self.lane_key.request_signature
            or evidence.generation != self.lane_key.generation
            or evidence.cursor_before != self.cursor_before
            or evidence.response_metadata != self.response_metadata
        ):
            raise ValueError("DurableProviderPage exhaustion evidence does not bind its lane")

    def to_journal(self) -> dict[str, Any]:
        """Serialize the authoritative durable-provider-page fields."""
        return {
            "page_id": self.page_id,
            "lane_key": self.lane_key.to_dict(),
            "request_cursor": self.cursor_before,
            "next_cursor": self.next_cursor,
            "returned_count": self.returned_count,
            "provider_exhausted": self.provider_exhausted,
            "exhaustion_evidence": None if self.exhaustion_evidence is None else self.exhaustion_evidence.to_dict(),
            "response_metadata": self.response_metadata.to_dict(),
            "state": self.journal_state,
            "candidates": list(self.candidates),
        }

    @classmethod
    def from_journal(cls, data: dict[str, Any]) -> "DurableProviderPage":
        """Deserialize only the active complete durable-page representation."""
        lane_key_data = data.get("lane_key")
        if not isinstance(lane_key_data, Mapping):
            raise ValueError("DurableProviderPage.lane_key is missing")
        lane_key = DiscoveryLaneKey.from_dict_strict(dict(lane_key_data))
        evidence_data = data.get("exhaustion_evidence")
        evidence = None
        if evidence_data is not None:
            if not isinstance(evidence_data, Mapping):
                raise ValueError("DurableProviderPage.exhaustion_evidence is invalid")
            evidence = ExhaustionEvidence.from_dict_strict(evidence_data)
        metadata_data = data.get("response_metadata")
        if not isinstance(metadata_data, Mapping):
            raise ValueError("DurableProviderPage.response_metadata is missing")
        meta = ProviderResponseMetadata.from_dict_strict(metadata_data)
        cursor = data.get("request_cursor")
        if not isinstance(cursor, str):
            raise ValueError("DurableProviderPage.request_cursor is invalid")
        returned = data.get("returned_count")
        if isinstance(returned, bool) or not isinstance(returned, int) or returned < 0:
            raise ValueError("DurableProviderPage.returned_count is invalid")

        return cls(
            page_id=str(data.get("page_id", "")),
            lane_key=lane_key,
            cursor_before=cursor,
            next_cursor=str(data["next_cursor"]) if data.get("next_cursor") is not None else None,
            returned_count=returned,
            provider_exhausted=bool(data["provider_exhausted"]),
            exhaustion_evidence=evidence,
            response_metadata=meta,
            journal_state=str(data.get("state", "")),
            candidates=tuple(data.get("candidates") or []),
        )

# ── GenerationHistoryEntry (Phase 9) ───────────────────────────────────

@dataclass(frozen=True)
class GenerationHistoryEntry:
    """Single typed entry in a backfill lane's generation history.

    Writer, reader, validator, and migration all reference this same model
    so the field set can never drift.
    """
    generation: int
    request_signature: str
    closed_at: str
    reason: str
    cursor: str
    exhausted: bool
    pages_succeeded: int
    pages_committed: int
    items_returned_total: int
    last_committed_page_id: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "generation": self.generation,
            "request_signature": self.request_signature,
            "closed_at": self.closed_at,
            "reason": self.reason,
            "cursor": self.cursor,
            "exhausted": self.exhausted,
            "pages_succeeded": self.pages_succeeded,
            "pages_committed": self.pages_committed,
            "items_returned_total": self.items_returned_total,
            "last_committed_page_id": self.last_committed_page_id,
        }

    @staticmethod
    def from_dict(d: dict[str, object]) -> "GenerationHistoryEntry":
        """Lenient parser for internal use — validates types, not exact keys."""
        def _require_int(key: str) -> int:
            val = d[key]
            if isinstance(val, bool) or not isinstance(val, int):
                raise TypeError(
                    f"GenerationHistoryEntry.{key} must be int, got {type(val).__name__}"
                )
            return val

        def _require_str(key: str) -> str:
            val = d.get(key, "")
            if not isinstance(val, str):
                raise TypeError(
                    f"GenerationHistoryEntry.{key} must be str, got {type(val).__name__}"
                )
            return val

        def _require_bool(key: str) -> bool:
            val = d[key]
            if not isinstance(val, bool):
                raise TypeError(
                    f"GenerationHistoryEntry.{key} must be bool, got {type(val).__name__}"
                )
            return val

        def _optional_str(key: str) -> str | None:
            val = d.get(key)
            if val is None:
                return None
            if not isinstance(val, str):
                raise TypeError(
                    f"GenerationHistoryEntry.{key} must be str or None, "
                    f"got {type(val).__name__}"
                )
            return val

        return GenerationHistoryEntry(
            generation=_require_int("generation"),
            request_signature=_require_str("request_signature"),
            closed_at=_require_str("closed_at"),
            reason=_require_str("reason"),
            cursor=_require_str("cursor"),
            exhausted=_require_bool("exhausted"),
            pages_succeeded=_require_int("pages_succeeded"),
            pages_committed=_require_int("pages_committed"),
            items_returned_total=_require_int("items_returned_total"),
            last_committed_page_id=_optional_str("last_committed_page_id"),
        )

    @staticmethod
    def from_dict_strict(d: dict[str, object]) -> "GenerationHistoryEntry":
        """Strict parser: exact keys only, no type coercion, no defaults.

        Raises ``ValueError`` on extra/missing keys, ``TypeError`` on wrong types.
        This is the validator used by notebook ``require_v4()``.
        """
        _ALLOWED_KEYS = frozenset({
            "generation", "request_signature", "closed_at", "reason",
            "cursor", "exhausted", "pages_succeeded", "pages_committed",
            "items_returned_total", "last_committed_page_id",
        })
        extra = set(d) - _ALLOWED_KEYS
        if extra:
            raise ValueError(
                f"GenerationHistoryEntry unknown fields: {sorted(extra)}"
            )
        missing = _ALLOWED_KEYS - set(d)
        if missing:
            raise ValueError(
                f"GenerationHistoryEntry missing fields: {sorted(missing)}"
            )
        return GenerationHistoryEntry.from_dict(d)

    def validate(self) -> None:
        errors: list[str] = []
        if self.generation < 1:
            errors.append(f"generation must be >= 1, got {self.generation}")
        if not self.request_signature:
            errors.append("request_signature must be non-blank")
        if not self.closed_at.strip():
            errors.append("closed_at must be non-blank")
        if not self.reason.strip():
            errors.append("reason must be non-blank")
        if self.pages_succeeded < 0:
            errors.append(f"pages_succeeded must be >= 0, got {self.pages_succeeded}")
        if self.pages_committed < 0:
            errors.append(f"pages_committed must be >= 0, got {self.pages_committed}")
        if self.items_returned_total < 0:
            errors.append(f"items_returned_total must be >= 0, got {self.items_returned_total}")
        if errors:
            from src.discovery.contracts.notebook import NotebookCorruptError
            raise NotebookCorruptError(
                f"GenerationHistoryEntry validation failed: {'; '.join(errors)}"
            )


# ── LaneExecutionSpec ─────────────────────────────────────────────────────


def _request_signature_hash(
    *,
    sort: str,
    filters: Mapping[str, object],
    page_size: int,
    pagination_schema_version: str,
) -> str:
    """Return the canonical, versioned request-signature digest.

    Keep this primitive here rather than duplicating it in the coordinator,
    page journal, and executors.  The exact byte representation intentionally
    matches the historical ``page_journal.request_signature`` contract.
    """
    payload = "\x1f".join((
        sort,
        json.dumps(_thaw_json_value(filters), ensure_ascii=False, sort_keys=True, allow_nan=False),
        str(page_size),
        pagination_schema_version,
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _freeze_json_value(value: object) -> object:
    """Deep-freeze a JSON value used by a request signature.

    ``MappingProxyType`` alone protects only the outer request-filter object.
    A nested mutable list or mapping could otherwise mutate a scheduled
    lane's transport identity after its hash has been checked.
    """
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise TypeError("RequestSignature filter keys must be str")
            frozen[key] = _freeze_json_value(nested)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("RequestSignature filters cannot contain non-finite floats")
        return value
    raise TypeError(
        "RequestSignature filters must contain only JSON scalar, mapping, or sequence values"
    )


def _thaw_json_value(value: object) -> object:
    """Return a detached JSON-compatible value from a frozen signature."""
    if isinstance(value, Mapping):
        return {str(key): _thaw_json_value(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return copy.deepcopy(value)


@dataclass(frozen=True)
class RequestSignature:
    """The one complete, immutable signature of a provider-page request.

    All five fields are required.  A signature is not a hash-only identifier:
    the complete payload is carried to journal persistence so recovery can
    prove that the cursor and request contract have not drifted.
    """

    sort: str
    filters: Mapping[str, object]
    page_size: int
    pagination_schema_version: str
    hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.sort, str):
            raise TypeError("RequestSignature.sort must be str")
        if not isinstance(self.filters, Mapping):
            raise TypeError("RequestSignature.filters must be a mapping")
        if isinstance(self.page_size, bool) or not isinstance(self.page_size, int) or self.page_size < 1:
            raise ValueError("RequestSignature.page_size must be a positive integer")
        if not isinstance(self.pagination_schema_version, str) or not self.pagination_schema_version.strip():
            raise ValueError("RequestSignature.pagination_schema_version must be non-blank")
        if not isinstance(self.hash, str) or len(self.hash) != 16:
            raise ValueError("RequestSignature.hash must be a 16-character digest")

        # Make the transport-affecting mapping immutable at the ownership
        # boundary.  A caller cannot mutate an already-scheduled spec.
        try:
            frozen_filters = _freeze_json_value(dict(self.filters))
        except (TypeError, ValueError) as exc:
            raise ValueError("RequestSignature.filters must be JSON serializable") from exc
        if not isinstance(frozen_filters, Mapping):  # defensive type narrowing
            raise TypeError("RequestSignature.filters must be a mapping")
        try:
            expected = _request_signature_hash(
                sort=self.sort,
                filters=frozen_filters,
                page_size=self.page_size,
                pagination_schema_version=self.pagination_schema_version,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("RequestSignature.filters must be JSON serializable") from exc
        if self.hash != expected:
            raise ValueError("RequestSignature.hash does not match its complete payload")
        object.__setattr__(self, "filters", frozen_filters)

    @classmethod
    def create(
        cls,
        *,
        sort: str | None,
        filters: Mapping[str, object] | None,
        page_size: int,
        pagination_schema_version: str = "2.0",
    ) -> "RequestSignature":
        """Build a canonical complete signature from resolved request fields."""
        normalized_sort = sort or ""
        normalized_filters = dict(filters or {})
        return cls(
            sort=normalized_sort,
            filters=normalized_filters,
            page_size=int(page_size),
            pagination_schema_version=pagination_schema_version,
            hash=_request_signature_hash(
                sort=normalized_sort,
                filters=normalized_filters,
                page_size=int(page_size),
                pagination_schema_version=pagination_schema_version,
            ),
        )

    @classmethod
    def from_dict_strict(cls, value: Mapping[str, object]) -> "RequestSignature":
        """Parse exactly the active persisted representation; no defaults."""
        expected = {"sort", "filters", "page_size", "pagination_schema_version", "hash"}
        extra = set(value) - expected
        missing = expected - set(value)
        if extra or missing:
            details: list[str] = []
            if extra:
                details.append(f"unexpected fields {sorted(extra)}")
            if missing:
                details.append(f"missing fields {sorted(missing)}")
            raise ValueError("RequestSignature " + "; ".join(details))
        sort = value["sort"]
        filters = value["filters"]
        page_size = value["page_size"]
        version = value["pagination_schema_version"]
        digest = value["hash"]
        if not isinstance(sort, str) or not isinstance(filters, Mapping):
            raise TypeError("RequestSignature sort and filters have invalid types")
        if isinstance(page_size, bool) or not isinstance(page_size, int):
            raise TypeError("RequestSignature.page_size must be int")
        if not isinstance(version, str) or not isinstance(digest, str):
            raise TypeError("RequestSignature version and hash must be str")
        return cls(
            sort=sort,
            filters=filters,
            page_size=page_size,
            pagination_schema_version=version,
            hash=digest,
        )

    # ``from_dict`` intentionally remains strict; callers must not revive
    # hash-only or partially specified journal records.
    from_dict = from_dict_strict

    def to_dict(self) -> dict[str, object]:
        return {
            "sort": self.sort,
            "filters": _thaw_json_value(self.filters),
            "page_size": self.page_size,
            "pagination_schema_version": self.pagination_schema_version,
            "hash": self.hash,
        }


@dataclass(frozen=True)
class LaneExecutionSpec:
    """Complete immutable execution input for one physical provider lane.

    The coordinator resolves the notebook profile, query, final generation,
    and request shape once.  Executors may read this object but must never
    construct a new key, filters, generation, or signature.
    """

    key: DiscoveryLaneKey
    request_signature: RequestSignature
    keyword_zh: str
    query: str
    query_language: Literal["zh", "en", "mixed"]
    relevance_profile_hash: str
    order: str | None = None
    topic_filter: str = ""
    refresh_run_id: str | None = None

    def __post_init__(self) -> None:
        if self.key.request_signature != self.request_signature.hash:
            raise ValueError("LaneExecutionSpec key/signature hash mismatch")
        if not self.keyword_zh.strip() or not self.query.strip():
            raise ValueError("LaneExecutionSpec keyword_zh and query must be non-blank")
        if self.query_language not in {"zh", "en", "mixed"}:
            raise ValueError("LaneExecutionSpec.query_language must be zh, en, or mixed")
        if not self.relevance_profile_hash:
            raise ValueError("LaneExecutionSpec.relevance_profile_hash must be non-blank")
        if self.key.mode == "refresh" and not self.refresh_run_id:
            raise ValueError("refresh LaneExecutionSpec requires refresh_run_id")

    @property
    def sort(self) -> str:
        return self.request_signature.sort

    @property
    def filters(self) -> Mapping[str, object]:
        return self.request_signature.filters

    @property
    def page_size(self) -> int:
        return self.request_signature.page_size
