"""Discovery v4 strict candidate contracts.

``PendingCandidateV4`` — candidates waiting to be processed.
``LegacyCandidateSeedV4`` — DOIs extracted from v2/v3 journals during migration.
Legacy seeds carry source provenance but NEVER advance cursors or provide
exhaustion evidence.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal, Mapping

# ── Schema versions ──────────────────────────────────────────────────────

CANDIDATE_SEED_SCHEMA_VERSION_V4 = "4.0"

# ── Origin markers ───────────────────────────────────────────────────────

CANDIDATE_ORIGIN_VALUES: frozenset[str] = frozenset({
    "provider_page", "legacy_candidate_seed", "manual_import",
})

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


# ── Pending candidate v4 ─────────────────────────────────────────────────


@dataclass(frozen=True)
class PendingCandidateV4:
    """A single candidate waiting to be processed in the v4 workspace."""

    candidate_id: str = ""
    keyword_id: str = ""
    origin: str = "provider_page"
    source_page_id: str | None = None
    doi: str | None = None
    normalized_doi: str | None = None
    title: str | None = None
    authors: list[str] | None = None
    year: int | None = None
    venue: str | None = None
    raw_provider_data: dict[str, Any] | None = None
    created_at: str = ""

    def __post_init__(self) -> None:
        if self.origin not in CANDIDATE_ORIGIN_VALUES:
            raise ValueError(f"invalid origin: {self.origin!r}")
        if self.year is not None:
            _check_type(self.year, int, "year")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "keyword_id": self.keyword_id,
            "origin": self.origin,
            "source_page_id": self.source_page_id,
            "doi": self.doi,
            "normalized_doi": self.normalized_doi,
            "title": self.title,
            "authors": self.authors,
            "year": self.year,
            "venue": self.venue,
            "raw_provider_data": self.raw_provider_data,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict_strict(cls, data: Mapping[str, Any]) -> "PendingCandidateV4":
        allowed = {
            "candidate_id", "keyword_id", "origin", "source_page_id",
            "doi", "normalized_doi", "title", "authors", "year", "venue",
            "raw_provider_data", "created_at",
        }
        extra = set(data) - allowed
        if extra:
            raise ValueError(f"PendingCandidateV4 unknown fields: {sorted(extra)}")

        origin = str(data.get("origin", "provider_page"))
        return cls(
            candidate_id=str(data.get("candidate_id", "")),
            keyword_id=str(data.get("keyword_id", "")),
            origin=origin,
            source_page_id=data.get("source_page_id"),
            doi=data.get("doi"),
            normalized_doi=data.get("normalized_doi"),
            title=data.get("title"),
            authors=list(data["authors"]) if isinstance(data.get("authors"), list) else None,
            year=data.get("year") if type(data.get("year")) is int and not isinstance(data.get("year"), bool) else None,
            venue=data.get("venue"),
            raw_provider_data=dict(data["raw_provider_data"]) if isinstance(data.get("raw_provider_data"), dict) else None,
            created_at=str(data.get("created_at", "")),
        )


# ── Legacy candidate seed v4 ─────────────────────────────────────────────


@dataclass(frozen=True)
class LegacyCandidateSeedV4:
    """A DOI extracted from a v2/v3 journal during migration.

    This is NEVER a v4 ProviderPageJournal.  Legacy seeds carry their
    source provenance but cannot advance cursors or provide exhaustion
    evidence.
    """

    seed_id: str
    doi: str
    normalized_doi: str
    keyword_id: str = ""
    keyword_zh: str = ""
    query_id: str = ""
    query_language: str = ""
    title: str | None = None
    provider: str = ""
    legacy_page_id: str = ""
    legacy_journal_sha256: str = ""
    source_schema_version: str = ""

    def __post_init__(self) -> None:
        if not self.seed_id or not self.seed_id.strip():
            raise ValueError("seed_id must be non-blank")
        if not self.doi or not self.doi.strip():
            raise ValueError("doi must be non-blank")
        if not self.normalized_doi or not self.normalized_doi.strip():
            raise ValueError("normalized_doi must be non-blank")
        if self.source_schema_version not in ("2.0", "3.0", ""):
            raise ValueError(
                f"source_schema_version must be '2.0' or '3.0', "
                f"got {self.source_schema_version!r}"
            )

    @staticmethod
    def compute_seed_id(legacy_page_id: str, normalized_doi: str) -> str:
        """Deterministic seed_id from legacy provenance."""
        return hashlib.sha256(
            f"{legacy_page_id}:{normalized_doi}".encode("utf-8")
        ).hexdigest()[:32]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CANDIDATE_SEED_SCHEMA_VERSION_V4,
            "seed_id": self.seed_id,
            "doi": self.doi,
            "normalized_doi": self.normalized_doi,
            "keyword_id": self.keyword_id,
            "keyword_zh": self.keyword_zh,
            "query_id": self.query_id,
            "query_language": self.query_language,
            "title": self.title,
            "provider": self.provider,
            "legacy_page_id": self.legacy_page_id,
            "legacy_journal_sha256": self.legacy_journal_sha256,
            "source_schema_version": self.source_schema_version,
        }

    @classmethod
    def from_dict_strict(cls, data: Mapping[str, Any]) -> "LegacyCandidateSeedV4":
        allowed = {
            "schema_version", "seed_id", "doi", "normalized_doi",
            "keyword_id", "keyword_zh", "query_id", "query_language",
            "title", "provider", "legacy_page_id", "legacy_journal_sha256",
            "source_schema_version",
        }
        extra = set(data) - allowed
        if extra:
            raise ValueError(f"LegacyCandidateSeedV4 unknown fields: {sorted(extra)}")
        return cls(
            seed_id=str(data["seed_id"]),
            doi=str(data["doi"]),
            normalized_doi=str(data["normalized_doi"]),
            keyword_id=str(data.get("keyword_id", "")),
            keyword_zh=str(data.get("keyword_zh", "")),
            query_id=str(data.get("query_id", "")),
            query_language=str(data.get("query_language", "")),
            title=data.get("title"),
            provider=str(data.get("provider", "")),
            legacy_page_id=str(data.get("legacy_page_id", "")),
            legacy_journal_sha256=str(data.get("legacy_journal_sha256", "")),
            source_schema_version=str(data.get("source_schema_version", "")),
        )
