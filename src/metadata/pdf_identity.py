"""Independent PDF/Markdown identity evidence extraction.

The extractor never accepts metadata values as fallbacks.  Its output is an
immutable snapshot that can be reproduced from the current PDF/conversion
assets when a match receipt is validated.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re

from src.discovery.models import normalize_doi
from src.utils.file_fingerprint import compute_sha256
from src.metadata.normalization import canonical_title


DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2}|21\d{2})\b")
CONFIDENCE_LEVELS = {
    "explicit_identifier",
    "structured_front_matter",
    "structured_pdf_metadata",
    "heuristic_text",
    "missing",
}


@dataclass(frozen=True)
class PdfIdentityEvidence:
    pdf_sha256: str
    extracted_dois: tuple[str, ...]
    canonical_title: str | None
    publication_year: int | None
    first_author_family: str | None
    author_families: tuple[str, ...]
    extraction_sources: tuple[str, ...]
    confidence: str
    warnings: tuple[str, ...]
    extracted_identifiers: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict:
        value = asdict(self)
        value["extracted_dois"] = list(self.extracted_dois)
        value["author_families"] = list(self.author_families)
        value["extraction_sources"] = list(self.extraction_sources)
        value["warnings"] = list(self.warnings)
        value["extracted_identifiers"] = [
            {"type": kind, "value": item} for kind, item in self.extracted_identifiers
        ]
        return value

    @classmethod
    def from_dict(cls, value: dict) -> "PdfIdentityEvidence":
        identifiers = value.get("extracted_identifiers") or []
        return cls(
            pdf_sha256=str(value.get("pdf_sha256") or ""),
            extracted_dois=tuple(str(x) for x in value.get("extracted_dois") or []),
            canonical_title=value.get("canonical_title"),
            publication_year=value.get("publication_year"),
            first_author_family=value.get("first_author_family"),
            author_families=tuple(str(x) for x in value.get("author_families") or []),
            extraction_sources=tuple(str(x) for x in value.get("extraction_sources") or []),
            confidence=str(value.get("confidence") or "missing"),
            warnings=tuple(str(x) for x in value.get("warnings") or []),
            extracted_identifiers=tuple(
                (str(x.get("type") or ""), str(x.get("value") or ""))
                for x in identifiers if isinstance(x, dict)
            ),
        )


def _front_matter_fields(text: str) -> tuple[str | None, int | None, tuple[str, ...]]:
    """Extract conservative identity fields from the physical first 100 lines."""
    lines = [line.strip() for line in text.splitlines()]
    headings = [line.lstrip("#").strip() for line in lines if line.startswith("#")]
    title = canonical_title(headings[0]) if headings else None
    years = [int(value) for value in YEAR_RE.findall("\n".join(lines[:40]))]
    year = years[0] if years else None
    families: list[str] = []
    for line in lines[1:25]:
        labelled = re.match(r"^(?:authors?|作者)\s*[:：]\s*(.+)$", line, re.I)
        if not labelled:
            continue
        author_text = labelled.group(1)
        for token in re.split(r"\s*(?:,|;|、|\band\b|&)\s*", author_text):
            token = token.strip()
            if not token:
                continue
            family = token.split()[-1].strip(". ")
            if family and len(family) >= 2:
                families.append(family)
    return title, year, tuple(dict.fromkeys(families))


def extract_pdf_identity_evidence(
    *,
    pdf_path: Path,
    markdown_path: Path | None = None,
    conversion_manifest_path: Path | None = None,
) -> PdfIdentityEvidence:
    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)
    text = ""
    sources: list[str] = []
    warnings: list[str] = []
    if markdown_path and markdown_path.is_file():
        text = "\n".join(
            markdown_path.read_text(encoding="utf-8", errors="ignore").splitlines()[:100]
        )
        sources.append("markdown.front_matter.first_100_lines")
    else:
        # DOI tokens are frequently embedded in the PDF text layer or XMP.  A
        # bounded byte scan supplies explicit identifier evidence without
        # consulting metadata and without invoking a network/PDF service.
        pdf_text = pdf_path.read_bytes()[: 2 * 1024 * 1024].decode("latin-1", errors="ignore")
        text = pdf_text
        sources.append("pdf.embedded_text.bounded_scan")
    if conversion_manifest_path and conversion_manifest_path.is_file():
        try:
            json.loads(conversion_manifest_path.read_text(encoding="utf-8"))
            sources.append("conversion.manifest")
        except (OSError, json.JSONDecodeError):
            warnings.append("conversion manifest is unreadable")

    dois = tuple(
        dict.fromkeys(
            normalized
            for raw in DOI_RE.findall(text)
            if (normalized := normalize_doi(raw))
        )
    )
    title, year, author_families = _front_matter_fields(text)
    if dois:
        confidence = "explicit_identifier"
    elif title and year and author_families:
        confidence = "structured_front_matter"
    elif text:
        confidence = "heuristic_text"
    else:
        confidence = "missing"
        warnings.append("no independent Markdown/PDF identity text evidence")
    return PdfIdentityEvidence(
        pdf_sha256=compute_sha256(pdf_path),
        extracted_dois=dois,
        canonical_title=title,
        publication_year=year,
        first_author_family=author_families[0] if author_families else None,
        author_families=author_families,
        extraction_sources=tuple(sources),
        confidence=confidence,
        warnings=tuple(warnings),
    )
