"""Independent PDF/Markdown identity evidence extraction (extractor v2).

The extractor never accepts metadata values as fallbacks.  Its output is an
immutable, deterministic snapshot that can be reproduced from the current
PDF/conversion assets when a match receipt is re-audited.

Evidence is structured (``DoiEvidence``) with a source tier, so the decision
layer can distinguish a first-page primary DOI from a reference-list DOI or a
raw-byte fragment.  Extraction order is fixed: XMP metadata, Document Info,
decoded text layer, Markdown front matter, then a bounded raw-byte scan that
is diagnostic only and can never alone drive an identity decision.  Parser
failures are collected (``parser_failures``); the overall confidence is
``unreadable`` only when a hard parse error exists AND no other evidence path
(XMP / Document Info / text layer / Markdown / structured identifiers)
produced usable identity information.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Literal

from src.utils.file_fingerprint import compute_sha256
from src.utils.identifiers import (
    extract_doi_candidates,
    join_line_broken_doi_lines,
)
from src.metadata.normalization import canonical_title

EXTRACTOR_VERSION = "2.0"

DOI_SOURCE = Literal[
    "xmp_metadata",
    "document_info",
    "first_page",
    "front_matter",
    "body_text",
    "reference_list",
    "raw_bytes",
]
DOI_CONFIDENCE = Literal["strong", "medium", "weak"]

YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2}|21\d{2})\b")
TEXT_LAYER_MAX_PAGES = 3
# A whole line naming the reference list; text from it onward on any page may
# carry foreign DOIs and is classified as reference-list evidence.  Leading
# markdown heading markers (## References) and trailing punctuation/colons
# are tolerated so the same rule works for PDF text and converted Markdown.
TEXT_LAYER_REFERENCES_RE = re.compile(
    r"^\s*#{0,3}\s*(references|bibliography|参考文献|literature cited)\s*[:.]?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
CONFIDENCE_LEVELS = {
    "explicit_identifier",
    "structured_front_matter",
    "heuristic_text",
    "missing",
    "unreadable",
}

# Context snippet length for deterministic evidence records.
CONTEXT_MAX_CHARS = 120

# XMP namespaces whose DOI-bearing elements are explicit structured keys.
_XMP_DC = "http://purl.org/dc/elements/1.1/"
_XMP_PRISM = "http://prismstandard.org/namespaces/basic/2.0/"
_XMP_PDFX = "http://ns.adobe.com/pdfx/1.3/"

# Document-Info keys that carry explicit DOI values (case-insensitive).
_DOCINFO_DOI_KEYS = {"doi", "prism:doi", "pdfx:doi", "dc:identifier"}
# Document-Info keys whose DOIs are at most medium evidence.
_DOCINFO_MEDIUM_KEYS = {"subject", "keywords"}

# Stable identifier patterns: (kind, regex); ISBN is a container identifier.
_IDENTIFIER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("arxiv", re.compile(r"\b(?:arXiv:|arXiv\s+)?(\d{4}\.\d{4,5})(?:v\d+)?\b", re.I)),
    ("handle", re.compile(r"\b(?:https?://)?(?:hdl\.)?handle\.net/(\d+\.\d+/[^\s<>\"]+)", re.I)),
    ("urn", re.compile(r"\burn:nbn:[^\s<>\"]+", re.I)),
    ("isbn", re.compile(r"\b(97[89][\-\s]?\d{1,5}[\-\s]?\d{1,7}[\-\s]?\d{1,6}[\-\s]?\d)\b")),
)


def _truncate_context(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()[:CONTEXT_MAX_CHARS]


@dataclass(frozen=True)
class DoiEvidence:
    """One DOI observation with its source tier.

    ``confidence`` is the extraction tier (explicit structured metadata keys
    are strong, labeled first-page DOIs strong, everything else medium/weak).
    The decision layer refines first-page strong evidence against the
    bibliographic strength; it never weakens XMP/Document-Info evidence.
    """

    doi: str
    source: DOI_SOURCE
    page_number: int | None
    labeled: bool
    context: str
    confidence: DOI_CONFIDENCE

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "DoiEvidence":
        return cls(
            doi=str(value.get("doi") or ""),
            source=str(value.get("source") or "raw_bytes"),
            page_number=value.get("page_number"),
            labeled=bool(value.get("labeled")),
            context=str(value.get("context") or ""),
            confidence=str(value.get("confidence") or "weak"),
        )


@dataclass(frozen=True)
class ExtractedIdentifier:
    """A structured non-DOI identifier: (type, kind, value).

    kind is ``work`` (arXiv ID, handle, URN, report number) or
    ``container`` (ISBN — identifies the whole book, not a chapter).
    """

    type: str
    kind: str
    value: str

    def to_dict(self) -> dict:
        return {"type": self.type, "kind": self.kind, "value": self.value}

    @classmethod
    def from_dict(cls, value: dict) -> "ExtractedIdentifier":
        return cls(
            type=str(value.get("type") or ""),
            kind=str(value.get("kind") or value.get("type") or "work"),
            value=str(value.get("value") or ""),
        )


@dataclass(frozen=True)
class PdfIdentityEvidence:
    pdf_sha256: str
    doi_evidence: tuple[DoiEvidence, ...]
    canonical_title: str | None
    publication_year: int | None
    first_author_family: str | None
    author_families: tuple[str, ...]
    extracted_identifiers: tuple[ExtractedIdentifier, ...]
    extraction_sources: tuple[str, ...]
    confidence: str
    parser_failures: tuple[str, ...]
    warnings: tuple[str, ...]
    identity_extractor_version: str = EXTRACTOR_VERSION

    def to_dict(self) -> dict:
        value = asdict(self)
        value["doi_evidence"] = [item.to_dict() for item in self.doi_evidence]
        value["author_families"] = list(self.author_families)
        value["extraction_sources"] = list(self.extraction_sources)
        value["parser_failures"] = list(self.parser_failures)
        value["warnings"] = list(self.warnings)
        value["extracted_identifiers"] = [
            item.to_dict() for item in self.extracted_identifiers
        ]
        return value

    @classmethod
    def from_dict(cls, value: dict) -> "PdfIdentityEvidence":
        """Restore evidence from a stored dict (v2 shape; v1 tolerated).

        v1 ``extracted_dois`` entries map to weak raw-byte diagnostics so
        legacy receipts never drive decisions under the v2 policy.
        """
        doi_evidence: list[DoiEvidence] = []
        for item in value.get("doi_evidence") or []:
            if isinstance(item, dict):
                doi_evidence.append(DoiEvidence.from_dict(item))
        if not doi_evidence:
            for item in value.get("extracted_dois") or []:
                doi_evidence.append(
                    DoiEvidence(
                        doi=str(item),
                        source="raw_bytes",
                        page_number=None,
                        labeled=False,
                        context="legacy extracted_dois",
                        confidence="weak",
                    )
                )
        identifiers: list[ExtractedIdentifier] = []
        for item in value.get("extracted_identifiers") or []:
            if isinstance(item, dict):
                identifiers.append(ExtractedIdentifier.from_dict(item))
        return cls(
            pdf_sha256=str(value.get("pdf_sha256") or ""),
            doi_evidence=tuple(doi_evidence),
            canonical_title=value.get("canonical_title"),
            publication_year=value.get("publication_year"),
            first_author_family=value.get("first_author_family"),
            author_families=tuple(str(x) for x in value.get("author_families") or []),
            extracted_identifiers=tuple(identifiers),
            extraction_sources=tuple(
                str(x) for x in value.get("extraction_sources") or []
            ),
            confidence=str(value.get("confidence") or "missing"),
            parser_failures=tuple(
                str(x) for x in value.get("parser_failures") or []
            ),
            warnings=tuple(str(x) for x in value.get("warnings") or []),
            identity_extractor_version=str(
                value.get("identity_extractor_version") or EXTRACTOR_VERSION
            ),
        )


# A capitalized-name line (author list on a PDF first page).  Lowercase
# words disqualify affiliation lines ("Department of Earth Sciences"), and
# single-word lines are section headers ("ARTICLE", "Abstract"), not names.
AUTHOR_LINE_RE = re.compile(
    r"^[A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+)+"
    r"(?:,\s*[A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+)*)*$"
)
# Common single-word section headers that must never be author families.
_SECTION_HEADER_WORDS = {
    "article", "abstract", "keywords", "introduction", "conclusions",
    "conclusion", "discussion", "results", "methods", "method", "references",
    "bibliography", "contents", "highlights", "summary", "acknowledgments",
    "acknowledgements", "supplementary", "appendix", "data", "doi",
    "received", "accepted", "published", "openaccess", "openaccess",
    "corresponding", "author", "authors", "research", "study", "paper",
}
_DOI_ONLY_LINE_RE = re.compile(r"(?i)(doi\s*[:=]|10\.\d{4,9}/)")


def _front_matter_fields(text: str) -> tuple[str | None, int | None, tuple[str, ...]]:
    """Extract conservative identity fields from the physical first 100 lines.

    Markdown headings are used when present; PDF text layers rarely carry
    them, so the first substantive non-DOI line is the title candidate.
    Author families come from an ``Authors:`` label (markdown) or from a
    capitalized-name line (PDF first page).
    """
    lines = [line.strip() for line in text.splitlines()]
    headings = [line.lstrip("#").strip() for line in lines if line.startswith("#")]
    if headings:
        title = canonical_title(headings[0])
    else:
        title = None
        for line in lines:
            if (
                line
                and len(line) > 8
                and not _DOI_ONLY_LINE_RE.search(line)
                and not YEAR_RE.search(line)
            ):
                title = canonical_title(line)
                break
    years = [int(value) for value in YEAR_RE.findall("\n".join(lines[:40]))]
    year = years[0] if years else None
    families: list[str] = []
    for line in lines[1:25]:
        labelled = re.match(r"^(?:authors?|作者)\s*[:：]\s*(.+)$", line, re.I)
        tokens = {token.strip(".").casefold() for token in line.split()}
        is_header = bool(tokens & _SECTION_HEADER_WORDS) and len(tokens) <= 4
        author_text = labelled.group(1) if labelled else (
            line
            if AUTHOR_LINE_RE.match(line)
            and not _DOI_ONLY_LINE_RE.search(line)
            and not is_header
            and not line.endswith(":")
            else None
        )
        if not author_text:
            continue
        for token in re.split(r"\s*(?:,|;|、|\band\b|&)\s*", author_text):
            token = token.strip()
            if not token:
                continue
            family = token.split()[-1].strip(". ")
            if family and len(family) >= 2:
                families.append(family)
    return title, year, tuple(dict.fromkeys(families))


def _identifiers_from_text(text: str) -> list[ExtractedIdentifier]:
    """Structured non-DOI identifiers from text (arXiv/handle/URN/ISBN)."""
    found: list[ExtractedIdentifier] = []
    for kind, pattern in _IDENTIFIER_PATTERNS:
        for match in pattern.finditer(text):
            raw = match.group(1) if match.groups() else match.group(0)
            value = raw.strip().lower()
            if kind == "arxiv":
                value = re.sub(r"^arxiv[: ]+", "", value)
            if not value:
                continue
            found.append(
                ExtractedIdentifier(
                    type=kind,
                    kind="work" if kind != "isbn" else "container",
                    value=value,
                )
            )
    # First-seen dedup preserving document order.
    seen: set[tuple[str, str, str]] = set()
    unique: list[ExtractedIdentifier] = []
    for item in found:
        key = (item.type, item.kind, item.value)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _xmp_evidence(doc: object, failures: list[str]) -> tuple[list[DoiEvidence], list[ExtractedIdentifier]]:
    """XMP metadata: explicit DOI-bearing elements are strong evidence."""
    import xml.etree.ElementTree as ET

    try:
        xml_text = doc.get_xml_metadata()  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover - fitz failure modes vary
        failures.append(f"xmp metadata unreadable: {exc}")
        return [], []
    if not xml_text or not xml_text.strip():
        return [], []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return [], []
    evidence: list[DoiEvidence] = []
    identifiers: list[ExtractedIdentifier] = []
    for element in root.iter():
        tag = element.tag
        local = tag.rsplit("}", 1)[-1] if "}" in tag else tag
        namespace = tag[1 : tag.index("}")] if tag.startswith("{") else ""
        text = element.text or ""
        if not text.strip():
            continue
        if namespace in {_XMP_PRISM, _XMP_PDFX} and local == "doi":
            tier: DOI_CONFIDENCE = "strong"
        elif namespace == _XMP_DC and local == "identifier":
            tier = "strong"
        elif namespace == _XMP_DC and local in {"subject", "description"}:
            tier = "medium"
        elif namespace == _XMP_PDFX and local == "keywords":
            tier = "medium"
        else:
            continue
        for candidate in extract_doi_candidates(text):
            evidence.append(
                DoiEvidence(
                    doi=candidate,
                    source="xmp_metadata",
                    page_number=None,
                    labeled=True,
                    context=f"xmp {local}",
                    confidence=tier,
                )
            )
        identifiers.extend(_identifiers_from_text(text))
    return evidence, identifiers


def _document_info_evidence(doc: object, failures: list[str]) -> tuple[list[DoiEvidence], list[ExtractedIdentifier]]:
    """Document Info: explicit DOI keys are strong; subject/keywords medium."""
    try:
        metadata = dict(doc.metadata or {})  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover
        failures.append(f"document info unreadable: {exc}")
        return [], []
    evidence: list[DoiEvidence] = []
    identifiers: list[ExtractedIdentifier] = []
    for key, value in metadata.items():
        if not isinstance(value, str) or not value.strip():
            continue
        lowered = key.strip().lower()
        if lowered in _DOCINFO_MEDIUM_KEYS:
            tier: DOI_CONFIDENCE = "medium"
        elif lowered in _DOCINFO_DOI_KEYS or "doi" in lowered:
            tier = "strong"
        else:
            continue
        for candidate in extract_doi_candidates(value):
            evidence.append(
                DoiEvidence(
                    doi=candidate,
                    source="document_info",
                    page_number=None,
                    labeled=tier == "strong",
                    context=f"docinfo {key}",
                    confidence=tier,
                )
            )
        identifiers.extend(_identifiers_from_text(value))
    return evidence, identifiers


def _text_layer_evidence(doc: object, failures: list[str]) -> tuple[list[DoiEvidence], list[ExtractedIdentifier], str]:
    """Decoded first-pages text layer, classified per line.

    Returns ``(evidence, identifiers, page1_text)``; ``page1_text`` is empty
    when the text layer is unavailable (hard failure recorded in
    ``failures``).
    """
    evidence: list[DoiEvidence] = []
    identifiers: list[ExtractedIdentifier] = []
    page1_text: list[str] = []
    in_references = False
    try:
        page_count = len(doc)  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover
        failures.append(f"PDF text layer is unreadable: {exc}")
        return [], [], ""
    for page_num in range(min(TEXT_LAYER_MAX_PAGES, page_count)):
        page = doc[page_num]  # type: ignore[attr-defined]
        page_1 = page_num == 0
        page_lines: list[str] = []
        try:
            blocks = page.get_text("dict").get("blocks", [])
        except Exception as exc:  # pragma: no cover
            failures.append(f"page {page_num + 1} text layer unreadable: {exc}")
            continue
        for block in blocks:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
                if not text:
                    continue
                if TEXT_LAYER_REFERENCES_RE.search(text):
                    in_references = True
                source: DOI_SOURCE = (
                    "reference_list"
                    if in_references
                    else "body_text"
                    if not page_1
                    else "first_page"
                )
                labeled = bool(
                    re.search(r"(?i)(doi\s*[:=]|https?://(?:dx\.)?doi\.org/)", text)
                )
                tier: DOI_CONFIDENCE = (
                    "weak"
                    if source in {"body_text", "reference_list"}
                    else "strong"
                    if labeled
                    else "medium"
                )
                for candidate in extract_doi_candidates(text):
                    evidence.append(
                        DoiEvidence(
                            doi=candidate,
                            source=source,
                            page_number=page_num + 1,
                            labeled=labeled,
                            context=_truncate_context(text),
                            confidence=tier,
                        )
                    )
                if page_1 and not in_references:
                    page1_text.append(text)
                    identifiers.extend(_identifiers_from_text(text))
                page_lines.append(text)
        # Line-broken DOIs: join across this page's lines, attribute to the
        # first line's classification (reference cutoff respected).
        for joined in join_line_broken_doi_lines(page_lines):
            source = (
                "reference_list"
                if in_references
                else "body_text"
                if not page_1
                else "first_page"
            )
            if source == "first_page" and joined not in [
                e.doi for e in evidence if e.source == "first_page"
            ]:
                evidence.append(
                    DoiEvidence(
                        doi=joined,
                        source="first_page",
                        page_number=page_num + 1,
                        labeled=True,
                        context="line-broken doi",
                        confidence="medium",
                    )
                )
    return evidence, identifiers, "\n".join(page1_text)


def _markdown_evidence(markdown_path: Path) -> tuple[list[DoiEvidence], list[ExtractedIdentifier], str]:
    """Markdown front matter (medium) and body/reference DOIs (weak).

    The reference-list boundary is found first and cuts the front matter,
    so short papers whose reference list sits inside the first 100 lines do
    not leak reference DOIs into the medium front-matter tier.
    """
    try:
        text = markdown_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return [], [], ""
    boundary_match = TEXT_LAYER_REFERENCES_RE.search(text)
    boundary = boundary_match.start() if boundary_match else len(text)
    front = "\n".join(text[:boundary].splitlines()[:100])
    evidence: list[DoiEvidence] = []
    identifiers = _identifiers_from_text(front)
    for candidate in extract_doi_candidates(front):
        evidence.append(
            DoiEvidence(
                doi=candidate,
                source="front_matter",
                page_number=None,
                labeled=True,
                context="markdown front matter",
                confidence="medium",
            )
        )
    body = text[:boundary][len(front):]
    references = text[boundary:]
    for candidate in extract_doi_candidates(body):
        evidence.append(
            DoiEvidence(
                doi=candidate,
                source="body_text",
                page_number=None,
                labeled=False,
                context="markdown body",
                confidence="weak",
            )
        )
    for candidate in extract_doi_candidates(references):
        evidence.append(
            DoiEvidence(
                doi=candidate,
                source="reference_list",
                page_number=None,
                labeled=False,
                context="markdown reference list",
                confidence="weak",
            )
        )
    return evidence, identifiers, front


def _byte_scan_evidence(pdf_path: Path, warnings: list[str]) -> list[DoiEvidence]:
    """Bounded raw-byte scan: diagnostic weak evidence only.

    Runs last and can never alone drive an identity decision; rejected
    fragments are counted into a warning.
    """
    try:
        pdf_text = pdf_path.read_bytes()[: 2 * 1024 * 1024].decode(
            "latin-1", errors="ignore"
        )
    except OSError as exc:
        warnings.append(f"raw byte scan unreadable: {exc}")
        return []
    candidates = extract_doi_candidates(pdf_text)
    raw_matches = len(re.findall(r"10\.\d{4,9}/[^\s<>\"')\]};,]{2,}", pdf_text))
    rejected = raw_matches - len(candidates)
    if rejected > 0:
        warnings.append(f"raw byte scan rejected {rejected} candidate fragments")
    return [
        DoiEvidence(
            doi=candidate,
            source="raw_bytes",
            page_number=None,
            labeled=False,
            context="bounded raw byte scan",
            confidence="weak",
        )
        for candidate in candidates
    ]


def extract_pdf_identity_evidence(
    *,
    pdf_path: Path,
    markdown_path: Path | None = None,
    conversion_manifest_path: Path | None = None,
) -> PdfIdentityEvidence:
    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)
    sources: list[str] = []
    warnings: list[str] = []
    failures: list[str] = []
    evidence: list[DoiEvidence] = []
    identifiers: list[ExtractedIdentifier] = []

    doc = None
    try:
        import fitz

        doc = fitz.open(str(pdf_path))
    except ImportError:
        failures.append("fitz (PyMuPDF) is unavailable for PDF text-layer extraction")
    except Exception as exc:
        failures.append(f"PDF text layer is unreadable: {exc}")

    front_text = ""
    if doc is not None:
        try:
            xmp_evidence, xmp_identifiers = _xmp_evidence(doc, failures)
            evidence.extend(xmp_evidence)
            identifiers.extend(xmp_identifiers)
            if xmp_evidence:
                sources.append("xmp_metadata")
            doc_info_evidence, doc_info_identifiers = _document_info_evidence(doc, failures)
            evidence.extend(doc_info_evidence)
            identifiers.extend(doc_info_identifiers)
            if doc_info_evidence:
                sources.append("document_info")
            layer_evidence, layer_identifiers, page1_text = _text_layer_evidence(doc, failures)
            evidence.extend(layer_evidence)
            identifiers.extend(layer_identifiers)
            if layer_evidence:
                sources.append("pdf.text_layer.first_pages")
            front_text = page1_text
        finally:
            doc.close()

    markdown_front = ""
    if markdown_path and markdown_path.is_file():
        md_evidence, md_identifiers, markdown_front = _markdown_evidence(markdown_path)
        evidence.extend(md_evidence)
        identifiers.extend(md_identifiers)
        sources.append("markdown.front_matter.first_100_lines")

    if conversion_manifest_path and conversion_manifest_path.is_file():
        try:
            json.loads(conversion_manifest_path.read_text(encoding="utf-8"))
            sources.append("conversion.manifest")
        except (OSError, json.JSONDecodeError):
            warnings.append("conversion manifest is unreadable")

    byte_evidence = _byte_scan_evidence(pdf_path, warnings)
    evidence.extend(byte_evidence)
    if byte_evidence:
        sources.append("pdf.raw_bytes.diagnostic")

    # Determinism: fixed sort key, dedup on (doi, source, page, labeled).
    def _key(item: DoiEvidence) -> tuple:
        return (
            item.doi,
            item.source,
            item.page_number if item.page_number is not None else -1,
            item.labeled,
        )

    seen: set[tuple] = set()
    ordered: list[DoiEvidence] = []
    for item in sorted(evidence, key=_key):
        key = _key(item)
        if key not in seen:
            seen.add(key)
            ordered.append(item)

    title, year, author_families = _front_matter_fields(
        markdown_front if markdown_front else front_text
    )

    has_usable = bool(ordered) or bool(identifiers) or bool(title or year or author_families)
    if failures and not has_usable:
        confidence = "unreadable"
    elif ordered:
        confidence = "explicit_identifier"
    elif title and year and author_families:
        confidence = "structured_front_matter"
    elif front_text or markdown_front:
        confidence = "heuristic_text"
    else:
        confidence = "missing"
        warnings.append("no independent Markdown/PDF identity text evidence")

    return PdfIdentityEvidence(
        pdf_sha256=compute_sha256(pdf_path),
        doi_evidence=tuple(ordered),
        canonical_title=title,
        publication_year=year,
        first_author_family=author_families[0] if author_families else None,
        author_families=author_families,
        extracted_identifiers=tuple(dict.fromkeys(identifiers)),
        extraction_sources=tuple(sources),
        confidence=confidence,
        parser_failures=tuple(failures),
        warnings=tuple(warnings),
        identity_extractor_version=EXTRACTOR_VERSION,
    )
