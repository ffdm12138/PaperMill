"""Serializable models for DOI discovery."""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import re
from typing import Any


DOI_PREFIX = "https://doi.org/"


def normalize_doi(doi: str | None) -> str:
    if not doi:
        return ""
    value = doi.strip()
    if value.lower().startswith(DOI_PREFIX):
        value = value[len(DOI_PREFIX):]
    return value.strip().lower()


def normalize_title(title: str | None) -> str:
    if not title:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", title.lower())).strip()


@dataclass
class PaperCandidate:
    title: str = ""
    year: int | None = None
    authors: list[str] = field(default_factory=list)
    doi: str = ""
    venue: str = ""
    abstract: str = ""
    source: str = ""
    source_id: str = ""
    url: str = ""
    pdf_url: str = ""
    open_access: bool = False
    citation_count: int | None = None
    confidence: float = 0.0
    query: str = ""
    domain_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    existing_duplicate_refs: list[dict[str, Any]] = field(default_factory=list)
    duplicate_indexed: bool = False

    def __post_init__(self) -> None:
        self.doi = normalize_doi(self.doi)
        if self.year is not None:
            try:
                self.year = int(self.year)
            except (TypeError, ValueError):
                self.year = None
        if not isinstance(self.authors, list):
            self.authors = [str(self.authors)]
        self.confidence = max(0.0, min(float(self.confidence or 0.0), 1.0))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["doi"] = normalize_doi(data.get("doi"))
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PaperCandidate":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class CandidateBatch:
    original_query: str
    expanded_queries: list[str]
    candidates: list[PaperCandidate]
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_query": self.original_query,
            "expanded_queries": self.expanded_queries,
            "created_at": self.created_at,
            "sources": self.sources,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CandidateBatch":
        return cls(
            original_query=data.get("original_query", ""),
            expanded_queries=list(data.get("expanded_queries") or []),
            created_at=data.get("created_at") or datetime.now(timezone.utc).isoformat(),
            sources=list(data.get("sources") or []),
            candidates=[
                PaperCandidate.from_dict(item)
                for item in data.get("candidates", [])
            ],
        )
