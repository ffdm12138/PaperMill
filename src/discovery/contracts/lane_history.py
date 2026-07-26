"""Lane history value contracts: response metadata, exhaustion, generations.

Pure frozen dataclasses with strict validation.  Extracted from
``execution.lane_models`` so the notebook contract can depend on them
without contracts importing execution (the documented contracts rule).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from src.discovery.contracts.errors import NotebookCorruptError


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
            raise NotebookCorruptError(
                f"GenerationHistoryEntry validation failed: {'; '.join(errors)}"
            )


