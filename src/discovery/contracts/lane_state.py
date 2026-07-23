"""Discovery v4 strict lane state contract.

Canonical ``LaneStateV4`` and ``CursorTransactionV4`` frozen dataclasses.
Cursor advancement requires a durable v4 page journal plus CAS
(expected_revision, expected_cursor) — there is no evidence-free API.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from src.discovery.constants import INITIAL_CURSOR
from src.discovery.contracts.enums import QueryLanguage

# ── Schema version ───────────────────────────────────────────────────────

LANE_STATE_SCHEMA_VERSION_V4 = "4.0"

# ── Validation helpers ───────────────────────────────────────────────────


def _check_type(value: Any, expected: type, field_name: str) -> None:
    if type(value) is not expected:
        if expected is int and isinstance(value, bool):
            raise TypeError(
                f"{field_name} must be int, got bool ({value!r})"
            )
        raise TypeError(
            f"{field_name} must be {expected.__name__}, "
            f"got {type(value).__name__} ({value!r})"
        )


# ── Lane state v4 ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LaneStateV4:
    """Frozen lane state stored per ``(keyword_id, query_id, provider, mode)``.

    Cursor advancement requires a durable v4 page journal plus expected
    revision and expected cursor — there is no evidence-free cursor API.
    """

    keyword_id: str = ""
    query_id: str = ""
    provider: str = ""  # "openalex" or "crossref"
    mode: str = ""       # "refresh" or "backfill"
    query_language: str = QueryLanguage.ZH.value
    cursor: str = INITIAL_CURSOR
    exhausted: bool = False
    generation: int = 1
    last_committed_page_id: str | None = None
    exhaustion_evidence_id: str | None = None
    revision: int = 0

    def __post_init__(self) -> None:
        # type checks
        _check_type(self.cursor, str, "cursor")  # but allow empty for init
        _check_type(self.exhausted, bool, "exhausted")
        _check_type(self.generation, int, "generation")
        _check_type(self.revision, int, "revision")

        if self.generation < 1:
            raise ValueError(f"generation must be >= 1, got {self.generation}")
        if self.revision < 0:
            raise ValueError(f"revision must be >= 0, got {self.revision}")
        if self.exhausted and self.exhaustion_evidence_id is None:
            raise ValueError(
                "exhausted lane requires exhaustion_evidence_id"
            )
        if not self.exhausted and self.exhaustion_evidence_id is not None:
            raise ValueError(
                "non-exhausted lane must not carry exhaustion_evidence_id"
            )
        if self.provider not in ("openalex", "crossref", ""):
            raise ValueError(f"provider must be openalex or crossref, got {self.provider!r}")
        if self.mode not in ("refresh", "backfill", ""):
            raise ValueError(f"mode must be refresh or backfill, got {self.mode!r}")

    @property
    def lane_key_str(self) -> str:
        """Compute stable lane key string for file naming."""
        return f"{self.keyword_id}_{self.query_id}_{self.provider}_{self.mode}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": LANE_STATE_SCHEMA_VERSION_V4,
            "keyword_id": self.keyword_id,
            "query_id": self.query_id,
            "provider": self.provider,
            "mode": self.mode,
            "query_language": self.query_language,
            "cursor": self.cursor,
            "exhausted": self.exhausted,
            "generation": self.generation,
            "last_committed_page_id": self.last_committed_page_id,
            "exhaustion_evidence_id": self.exhaustion_evidence_id,
            "revision": self.revision,
        }

    @classmethod
    def from_dict_strict(cls, data: Mapping[str, Any]) -> "LaneStateV4":
        allowed = {
            "schema_version", "keyword_id", "query_id", "provider", "mode",
            "query_language", "cursor", "exhausted", "generation",
            "last_committed_page_id", "exhaustion_evidence_id", "revision",
        }
        extra = set(data) - allowed
        if extra:
            raise ValueError(f"LaneStateV4 unknown fields: {sorted(extra)}")

        sv = data.get("schema_version")
        if sv != LANE_STATE_SCHEMA_VERSION_V4:
            raise ValueError(f"schema_version must be {LANE_STATE_SCHEMA_VERSION_V4!r}")

        exhausted = data.get("exhausted", False)
        if type(exhausted) is not bool:
            raise TypeError(f"exhausted must be bool, got {type(exhausted).__name__}")

        generation = data.get("generation", 1)
        if type(generation) is not int or isinstance(generation, bool):
            raise TypeError(f"generation must be int, got {type(generation).__name__}")

        revision = data.get("revision", 0)
        if type(revision) is not int or isinstance(revision, bool):
            raise TypeError(f"revision must be int, got {type(revision).__name__}")

        return cls(
            keyword_id=str(data.get("keyword_id", "")),
            query_id=str(data.get("query_id", "")),
            provider=str(data.get("provider", "")),
            mode=str(data.get("mode", "")),
            query_language=str(data.get("query_language", QueryLanguage.ZH.value)),
            cursor=str(data.get("cursor", INITIAL_CURSOR)),
            exhausted=exhausted,
            generation=generation,
            last_committed_page_id=data.get("last_committed_page_id"),
            exhaustion_evidence_id=data.get("exhaustion_evidence_id"),
            revision=revision,
        )


# ── Cursor transaction v4 ────────────────────────────────────────────────


@dataclass(frozen=True)
class CursorTransactionV4:
    """Durable cursor advancement for one lane.

    Requires: a durable v4 page journal path, expected revision, and
    expected cursor.  The new cursor and new revision MUST agree with
    the page journal's ``next_cursor``.
    """

    keyword_id: str = ""
    query_id: str = ""
    provider: str = ""
    mode: str = ""
    expected_revision: int = 0
    expected_cursor: str = INITIAL_CURSOR
    new_cursor: str = INITIAL_CURSOR
    new_revision: int = 1
    page_path: str = ""
    committed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def __post_init__(self) -> None:
        _check_type(self.expected_revision, int, "expected_revision")
        _check_type(self.new_revision, int, "new_revision")

        if self.expected_revision < 0:
            raise ValueError(
                f"expected_revision must be >= 0, got {self.expected_revision}"
            )
        if self.new_revision <= self.expected_revision:
            raise ValueError(
                f"new_revision ({self.new_revision}) must be > "
                f"expected_revision ({self.expected_revision})"
            )
        if not isinstance(self.expected_cursor, str) or not self.expected_cursor.strip():
            raise ValueError("expected_cursor must be non-blank str")
        if not isinstance(self.new_cursor, str) or not self.new_cursor.strip():
            raise ValueError("new_cursor must be non-blank str")

    @property
    def lane_key_str(self) -> str:
        return f"{self.keyword_id}_{self.query_id}_{self.provider}_{self.mode}"
