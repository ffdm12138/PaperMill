"""Legacy discovery contracts used only by the v4 migration tool.

These types describe v2/v3 on-disk data and transient migration seeds.  They
must never be imported by production discovery runtime code.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping


CANDIDATE_SEED_SCHEMA_VERSION_V4 = "4.0"


@dataclass(frozen=True)
class LegacyCandidateSeedV4:
    """A DOI extracted from a legacy page journal during migration.

    This is NEVER a v4 ProviderPageJournal.  Legacy seeds carry their
    source provenance but cannot advance cursors or provide exhaustion
    evidence.  Seeds are produced by
    ``src.migrations.discovery_v4.candidate_extraction`` from strictly
    validated ``LegacyCandidateV3`` records and consumed when building
    ``PendingCandidateV4`` entries for the staging workspace.
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
    legacy_candidate_id: str = ""
    lane: str = ""
    query: str = ""
    authors: tuple[str, ...] | None = None
    year: int | None = None
    venue: str | None = None

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
        if self.year is not None and (
            isinstance(self.year, bool) or not isinstance(self.year, int)
        ):
            raise ValueError("year must be an integer or None")

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
            "legacy_candidate_id": self.legacy_candidate_id,
            "lane": self.lane,
            "query": self.query,
            "authors": list(self.authors) if self.authors is not None else None,
            "year": self.year,
            "venue": self.venue,
        }

    @classmethod
    def from_dict_strict(cls, data: Mapping[str, Any]) -> "LegacyCandidateSeedV4":
        allowed = {
            "schema_version", "seed_id", "doi", "normalized_doi",
            "keyword_id", "keyword_zh", "query_id", "query_language",
            "title", "provider", "legacy_page_id", "legacy_journal_sha256",
            "source_schema_version", "legacy_candidate_id", "lane", "query",
            "authors", "year", "venue",
        }
        extra = set(data) - allowed
        if extra:
            raise ValueError(f"LegacyCandidateSeedV4 unknown fields: {sorted(extra)}")
        authors_value = data.get("authors")
        year_value = data.get("year")
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
            legacy_candidate_id=str(data.get("legacy_candidate_id", "")),
            lane=str(data.get("lane", "")),
            query=str(data.get("query", "")),
            authors=(
                tuple(str(a) for a in authors_value)
                if isinstance(authors_value, list)
                else None
            ),
            year=(
                year_value
                if type(year_value) is int
                else None
            ),
            venue=data.get("venue"),
        )
