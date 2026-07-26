"""Discovery v4 strict candidate contracts.

``PendingCandidateV4`` — candidates waiting to be processed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

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


__all__ = ["CANDIDATE_ORIGIN_VALUES", "PendingCandidateV4"]
