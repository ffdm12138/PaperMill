"""Serializable models for DOI discovery."""
from dataclasses import asdict, dataclass, field
from typing import Any

from src.utils.identifiers import normalize_doi


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
    doi_resolution: dict[str, Any] = field(default_factory=dict)
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

