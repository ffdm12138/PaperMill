"""paper_raw metadata resolver — resolve metadata candidates for unmatched PDFs.

This module closes the PDF-first gap: for manual PDF imports, MinerU conversion
must produce ``data/paper_raw/<paper_number>/<paper_number>.md`` before metadata
resolution runs. The converted Markdown is the primary evidence for DOI/title/
author/year/venue candidates; PDF filename and PDF text are optional hints, never
the sole metadata source. With ``--allow-network`` the resolver verifies extracted
candidates online or searches online when local candidates are missing. It produces
scored candidates with evidence.

The LLM-facing resolver and deterministic candidate-selection apply step never
write embedded match state or the authoritative match receipt. They emit/select
pure bibliographic candidates only. Independent PDF identity extraction then
writes `<paper_number>.metadata_match.json`; Metadata freeze replays it. The
``apply`` fills ONLY empty bibliographic fields (via ``merge_missing_metadata``).

Hard rules:
- Never fabricate DOI/author/year/venue/volume/pages. Facts come only from an
  authoritative source (Crossref/OpenAlex), the PDF/Markdown text,
  or a human ``--manual-confirm``.
- No-DOI candidates can never become matched.
- Network-title-search candidates can NEVER be auto-matched; only ``manual_confirmed``
  via ``--manual-confirm --apply`` after passing the full gate.
- Non-empty metadata fields are never overwritten (delegated to merge_missing_metadata).
- Identifier conflicts cannot be overridden by candidate selection or ordinary manual confirmation.
- Intermediate states live in side files (``.import_status.json``,
  ``<id>.metadata.candidates.json``, ``<id>.metadata.resolve_report.json``).

Reuses existing code (do not duplicate):
- ``src.discovery.models.normalize_doi/normalize_title/PaperCandidate``
- ``src.discovery.resolve_crossref`` (title search + DOI lookup)
- ``src.discovery.search_openalex`` (network keyword search / verification)
- ``src.services.metadata_enrichment_service`` (DOI extraction + Crossref enrichment)
- ``src.services.markdown_metadata_extractor`` (Markdown candidate extraction)
- ``src.metadata.schema`` / ``src.metadata.normalization``
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from loguru import logger
from filelock import FileLock

from config.settings import PAPER_RAW_DIR, PAPERS_DIR
from src.ingest.import_status import write_import_status
from src.ingest.locking import paper_raw_write_lock
from src.discovery.models import PaperCandidate
from src.utils.jsonio import read_json
from src.utils.identifiers import collect_dois_from_text, normalize_doi, normalize_title
from src.discovery.providers.provider_errors import ProviderRateLimited
from src.discovery.resolve_crossref import (
    get_crossref_work_by_doi,
    resolve_crossref_by_title,
)
from src.discovery.search_openalex import search_openalex
from src.file_fingerprint import compute_sha256
from src.ingest.duplicate_guard import check_doi_duplicate, check_pdf_duplicate
from src.services.markdown_metadata_extractor import (
    extract_front_matter_candidates_from_markdown,
    extract_metadata_from_markdown,
)
from src.services import metadata_enrichment_service as mes
from src.services.metadata_enrichment_service import (
    EnrichmentResult,
    enrich_from_doi,
    extract_doi_from_filename,
    extract_doi_from_pdf_file,
)
from src.metadata.quality import bibliographic_identity_gate
from src.utils.rate_limit import ProviderRateLimiter, default_config
from src.metadata.schema import empty_metadata, first_author_family, metadata_doi, validate_metadata_schema
from src.metadata.normalization import merge_missing_metadata
from src.metadata.source_records import write_metadata_source_record
from src.utils.atomic_io import atomic_write_json
from src.utils.timestamps import now_iso as _now_iso


# ── Constants ──────────────────────────────────────────────────────────

AUTHORITATIVE_DOI_SOURCES = {"filename", "pdf", "markdown", "metadata"}
NETWORK_DOI_SOURCES = {"network_title"}
REFERENCES_HEADING_RE = re.compile(r"^\s{0,6}#{1,6}\s*(references|bibliography|参考文献)", re.IGNORECASE)
MD_HEADER_SCAN_CHARS = 15000
MD_HEADER_SCAN_LINES = 100

# Decision bands
AUTO_MATCH_THRESHOLD = 0.88
MANUAL_REVIEW_THRESHOLD = 0.70

# .import_status.json statuses (free-form; metadata_match.status enum unchanged)
STATUS_CANDIDATES_FOUND = "metadata_candidates_found"
STATUS_RESOLVE_FAILED = "metadata_resolve_failed"
STATUS_CANDIDATE_CONFLICT = "metadata_candidate_conflict"
STATUS_MATCHED = "metadata_matched"
STATUS_MANUAL_REVIEW = "metadata_manual_review_required"



# ── Dataclasses ────────────────────────────────────────────────────────

@dataclass
class ResolvedCandidate:
    candidate_id: str
    doi: str
    title: str
    authors: list[str]
    year: int | None
    venue: str
    source: str            # crossref|openalex|markdown|pdf_text|filename|network_title (semantic_scholar legacy-tolerated)
    doi_source: str        # filename|pdf|markdown|network_title
    confidence: float
    score: float
    score_components: dict[str, float]
    doi_source_conf: float
    authoritative: bool
    decision: str          # auto_matched | manual_review | rejected
    gate_reasons: list[str]
    evidence: list[str]
    warnings: list[str]
    patch: dict

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "doi": self.doi,
            "title": self.title,
            "authors": self.authors,
            "year": self.year,
            "venue": self.venue,
            "source": self.source,
            "doi_source": self.doi_source,
            "confidence": self.confidence,
            "score": self.score,
            "score_components": self.score_components,
            "doi_source_conf": self.doi_source_conf,
            "authoritative": self.authoritative,
            "decision": self.decision,
            "gate_reasons": self.gate_reasons,
            "evidence": self.evidence,
            "warnings": self.warnings,
            "patch": self.patch,
        }


@dataclass
class ResolveReport:
    source_id: str
    folder: str
    metadata_path: str
    existing_doi: str
    doi_source: str          # metadata|filename|pdf|markdown|crossref_doi|network_title|none|conflict
    local_title: str
    local_year: int | None
    local_first_author_family: str
    pdf_sha256: str
    candidates: list[ResolvedCandidate]
    best_candidate_id: str | None
    decision: str            # auto_matched | manual_review | rejected | no_candidates | conflict
    reason: str
    warnings: list[str]
    created_at: str
    applied: bool
    applied_status: str      # matched | manual_confirmed | ""
    chosen_candidate_id: str | None = None
    title_source: str = "none"
    author_source: str = "none"
    markdown_front_matter_max_lines: int = 100
    markdown_front_matter_lines: list[str] = field(default_factory=list)
    local_title_evidence_missing: bool = False
    local_doi_candidates: list[str] = field(default_factory=list)
    post_conversion: bool = False

    def used_markdown(self) -> bool:
        """True when converted Markdown actually contributed evidence."""
        return (
            self.doi_source == "markdown"
            or self.title_source == "markdown_front_matter"
            or self.author_source == "markdown_front_matter"
        )

    def metadata_sources(self) -> list[str]:
        """Normalized set of sources that contributed evidence (deduped, sorted)."""
        raw = {self.doi_source, self.title_source, self.author_source}
        normalized: set[str] = set()
        for src in raw:
            if not src or src in {"none", "conflict"}:
                continue
            if src.startswith("markdown"):
                normalized.add("markdown")
            elif src.startswith("pdf"):
                normalized.add("pdf")
            elif src == "metadata":
                normalized.add("metadata")
            elif src == "filename":
                normalized.add("filename")
            elif src == "network_title":
                normalized.add("network")
            else:
                normalized.add(src)
        return sorted(normalized)

    def to_dict(self) -> dict:
        local_evidence = {
            "doi_candidates": self.local_doi_candidates,
            "title_candidates": [self.local_title] if self.local_title else [],
            "author_candidates": [self.local_first_author_family] if self.local_first_author_family else [],
            "title_source": self.title_source,
            "author_source": self.author_source,
            "doi_source": self.doi_source,
            "markdown_front_matter_max_lines": self.markdown_front_matter_max_lines,
            "markdown_front_matter_lines": self.markdown_front_matter_lines,
            "local_title_evidence_missing": self.local_title_evidence_missing,
        }
        return {
            "paper_number": self.source_id,
            "paper_raw_id": self.source_id,
            "folder": self.folder,
            "metadata_path": self.metadata_path,
            "existing_doi": self.existing_doi,
            "doi_source": self.doi_source,
            "local_title": self.local_title,
            "local_year": self.local_year,
            "local_first_author_family": self.local_first_author_family,
            "pdf_sha256": self.pdf_sha256,
            "candidates": [c.to_dict() for c in self.candidates],
            "best_candidate_id": self.best_candidate_id,
            "decision": self.decision,
            "reason": self.reason,
            "warnings": self.warnings,
            "created_at": self.created_at,
            "applied": self.applied,
            "applied_status": self.applied_status,
            "chosen_candidate_id": self.chosen_candidate_id,
            "title_source": self.title_source,
            "author_source": self.author_source,
            "markdown_front_matter_max_lines": self.markdown_front_matter_max_lines,
            "markdown_front_matter_lines": self.markdown_front_matter_lines,
            "used_markdown": self.used_markdown(),
            "metadata_sources": self.metadata_sources(),
            "post_conversion": self.post_conversion,
            "local_evidence": local_evidence,
            "decision_detail": {
                "status": self.decision,
                "reason": self.reason,
                "can_commit": self.applied and self.applied_status in {"matched", "manual_confirmed"},
            },
        }


# ── Formal-library duplicate sets ──────────────────────────────────────

_read_json = read_json


# ── Name helpers (conservative author split) ───────────────────────────

def _ascii_fold(value: str) -> str:
    nfkd = unicodedata.normalize("NFKD", value)
    return nfkd.encode("ascii", "ignore").decode("ascii")


def _is_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def _surname(name: str) -> str:
    """Ascii-folded, lowercased last token of a name (for matching only)."""
    if not name:
        return ""
    folded = _ascii_fold(name).strip()
    if not folded:
        return ""
    token = re.split(r"[\s,]+", folded)
    token = [t for t in token if t]
    if not token:
        return ""
    return re.sub(r"[^a-z0-9]", "", token[-1].lower())


def _split_name(name: str) -> tuple[str, str]:
    """Conservative (family, given) split. Returns ("", "") when unreliable.

    Assumes Western "Given Family" order (as returned by OpenAlex/S2 display
    names). Unreliable cases (return ("","") so the caller stores full_name
    only): empty, single token, CJK characters, all-caps institution-like
    strings, or when the last token is a single initial (ambiguous "Family G"
    citation form — we refuse to guess). Never fabricate a wrong family name.
    """
    if not name:
        return "", ""
    name = name.strip()
    if not name or _is_cjk(name):
        return "", ""
    parts = re.split(r"\s+", name)
    if len(parts) < 2:
        return "", ""
    if name.isupper() and len(name) <= 6:
        return "", ""
    last = parts[-1]
    # single-letter initial as last token → ambiguous citation form, refuse
    if len(last) == 1:
        return "", ""
    family = last
    given = " ".join(parts[:-1])
    return family, given


# ── Patch builders ─────────────────────────────────────────────────────

def patch_from_enrichment(source_id: str, result: EnrichmentResult) -> dict:
    """Flat EnrichmentResult → nested empty_metadata subset (promoted copy of
    scripts/resolve_paper_raw_metadata.py metadata enrichment)."""
    patch = empty_metadata(source_id, source_type="metadata_resolution")
    if getattr(result, "title", ""):
        patch["title"]["original"] = result.title
    if getattr(result, "year", None) is not None:
        patch["year"] = result.year
    if getattr(result, "doi", ""):
        patch["identifiers"]["doi"] = result.doi
    if getattr(result, "venue", ""):
        patch["container"]["journal"] = result.venue
    if getattr(result, "publisher", ""):
        patch["container"]["publisher"] = result.publisher
    for attr, key in (
        ("volume", "volume"),
        ("number", "number"),
        ("issue", "issue"),
        ("pages", "pages"),
        ("article_number", "article_number"),
    ):
        value = getattr(result, attr, "")
        if value:
            patch["publication"][key] = str(value)
    if not patch["publication"]["number"] and patch["publication"]["issue"]:
        patch["publication"]["number"] = patch["publication"]["issue"]
    if not patch["publication"]["issue"] and patch["publication"]["number"]:
        patch["publication"]["issue"] = patch["publication"]["number"]
    if getattr(result, "issn", ""):
        patch["identifiers"]["issn"] = result.issn
    if getattr(result, "url", ""):
        patch["links"]["url"] = result.url
    if getattr(result, "published", ""):
        patch["date"]["published"] = result.published
    authors = getattr(result, "authors", None) or []
    if authors:
        normalized = []
        for author in authors:
            if isinstance(author, dict):
                full = author.get("full_name") or author.get("name") or ""
                fam = author.get("family") or ""
                giv = author.get("given") or ""
                if not fam and not giv and full:
                    fam, giv = _split_name(full)
                normalized.append({
                    "full_name": full,
                    "family": fam,
                    "given": giv,
                    "orcid": author.get("orcid") or "",
                    "affiliation": author.get("affiliation") or "",
                })
            else:
                full = str(author)
                fam, giv = _split_name(full)
                if not fam and full and len(full.split()) == 1:
                    fam = full
                normalized.append({"full_name": full, "family": fam, "given": giv, "orcid": "", "affiliation": ""})
        patch["authors"] = normalized
        first = normalized[0]
        patch["first_author"] = {"family": first.get("family", ""), "display": first.get("full_name", "")}
    patch["source"] = {
        "kind": "metadata_resolution",
        "provider": getattr(result, "source", "") or "",
        "query": "",
        "retrieved_at": _now_iso(),
        "raw_record_path": f"source_records/metadata_source.{getattr(result, 'source', '') or 'metadata_resolution'}.json",
    }
    return patch


def patch_from_candidate(source_id: str, candidate: PaperCandidate) -> dict:
    """PaperCandidate (authors: list[str]) → nested patch with conservative split."""
    patch = empty_metadata(source_id, source_type="metadata_resolution")
    patch.pop("metadata_match", None)
    if candidate.title:
        patch["title"]["original"] = candidate.title
    if candidate.year is not None:
        patch["year"] = candidate.year
    if candidate.doi:
        patch["identifiers"]["doi"] = candidate.doi
    if candidate.venue:
        patch["container"]["journal"] = candidate.venue
    if candidate.url:
        patch["links"]["url"] = candidate.url
    if candidate.authors:
        normalized = []
        for name in candidate.authors:
            full = str(name)
            fam, giv = _split_name(full)
            normalized.append({"full_name": full, "family": fam, "given": giv, "orcid": "", "affiliation": ""})
        patch["authors"] = normalized
        first = normalized[0]
        patch["first_author"] = {"family": first.get("family", ""), "display": first.get("full_name", "")}
    patch["source"] = {
        "kind": "metadata_resolution",
        "provider": candidate.source or "",
        "query": candidate.query or "",
        "retrieved_at": _now_iso(),
        "raw_record_path": f"source_records/metadata_source.{candidate.source or 'metadata_resolution'}.json",
    }
    return patch


# ── Scoring ────────────────────────────────────────────────────────────

def score_candidate(
    *,
    candidate_title: str,
    candidate_year: int | None,
    candidate_authors: list[str],
    candidate_venue: str,
    local_title: str,
    local_year: int | None,
    local_first_author_family: str,
    doi_source_conf: float,
) -> tuple[float, dict[str, float]]:
    """Score = 0.40*title + 0.20*author + 0.15*year + 0.15*doi_src + 0.10*venue.

    Score only ranks candidates / assigns the auto/manual band. It is NEVER the
    source of a metadata fact.
    """
    title_sim = SequenceMatcher(
        None, normalize_title(local_title), normalize_title(candidate_title)
    ).ratio() if (local_title and candidate_title) else 0.0

    author_sim = 0.0
    if local_first_author_family and candidate_authors:
        cand_first_surname = _surname(candidate_authors[0]) if candidate_authors else ""
        if cand_first_surname and cand_first_surname == local_first_author_family:
            author_sim = 1.0

    year_match = 0.0
    if candidate_year is not None and local_year is not None:
        if candidate_year == local_year:
            year_match = 1.0
        elif abs(candidate_year - local_year) <= 1:
            year_match = 0.5

    venue_presence = 1.0 if (candidate_venue or "").strip() else 0.0

    score = (
        0.40 * title_sim
        + 0.20 * author_sim
        + 0.15 * year_match
        + 0.15 * doi_source_conf
        + 0.10 * venue_presence
    )
    components = {
        "title_sim": round(title_sim, 4),
        "author_sim": round(author_sim, 4),
        "year_match": round(year_match, 4),
        "doi_source_conf": round(doi_source_conf, 4),
        "venue_presence": round(venue_presence, 4),
    }
    return round(score, 4), components


# ── Auto-match gate ────────────────────────────────────────────────────

def _authoritative_source_complete(result: EnrichmentResult | None, candidate: PaperCandidate | None) -> bool:
    """Authoritative source returned complete title/authors/year/venue/doi."""
    if result is not None:
        return bool(
            getattr(result, "title", "")
            and getattr(result, "year", None) is not None
            and (getattr(result, "authors", None) or [])
            and getattr(result, "venue", "")
            and getattr(result, "doi", "")
        )
    if candidate is not None:
        return bool(
            candidate.title
            and candidate.year is not None
            and candidate.authors
            and candidate.venue
            and candidate.doi
        )
    return False


def auto_match_gate(
    *,
    doi: str,
    doi_source: str,
    resolvable: bool,
    candidate_title: str,
    candidate_year: int | None,
    candidate_authors: list[str],
    candidate_venue: str,
    local_title: str,
    local_year: int | None,
    local_first_author_family: str,
    existing_doi: str,
    authoritative_complete: bool,
) -> tuple[bool, list[str]]:
    """Return (passes, reasons). All conditions must hold for auto-match.

    Network-title candidates never pass (doi_source not in authoritative set).
    Local-evidence fallback: a local field check applies only when that local
    field exists; missing local evidence falls back to requiring authoritative
    source completeness (not a failure).
    """
    reasons: list[str] = []
    if doi_source not in AUTHORITATIVE_DOI_SOURCES:
        reasons.append(f"doi_source '{doi_source}' not authoritative (filename/pdf/markdown)")
        return False, reasons
    if not doi or "/" not in doi:
        reasons.append("doi missing or malformed")
        return False, reasons
    if not resolvable:
        reasons.append("doi not resolvable by Crossref/OpenAlex")
        return False, reasons
    if existing_doi and normalize_doi(existing_doi) != normalize_doi(doi):
        reasons.append(f"doi conflict: existing {existing_doi} vs candidate {doi}")
        return False, reasons
    if not (candidate_venue or "").strip():
        reasons.append("venue empty")
        return False, reasons

    # Local-evidence fallback checks (only when local evidence present)
    if local_title:
        title_sim = SequenceMatcher(
            None, normalize_title(local_title), normalize_title(candidate_title)
        ).ratio()
        if not (title_sim >= 0.85 or authoritative_complete):
            reasons.append(f"title similarity {title_sim:.2f} < 0.85 and authoritative source incomplete")
            return False, reasons
    if local_year is not None and candidate_year is not None:
        if abs(candidate_year - local_year) > 1:
            reasons.append(f"year {candidate_year} not within +/-1 of local {local_year}")
            return False, reasons
    if local_first_author_family and candidate_authors:
        cand_surname = _surname(candidate_authors[0])
        if not cand_surname or cand_surname != local_first_author_family:
            reasons.append(
                f"first author surname '{cand_surname}' does not match local '{local_first_author_family}'"
            )
            return False, reasons
    # If local evidence absent, require authoritative completeness
    if not local_title and not local_year and not local_first_author_family:
        if not authoritative_complete:
            reasons.append("local evidence absent and authoritative source incomplete")
            return False, reasons
    return True, reasons


def _duplicate_candidate_reasons(
    doi: str,
    *,
    paper_raw_dir: Path,
    papers_dir: Path,
    skip_paper_number: str,
) -> list[str]:
    reasons: list[str] = []
    dup = check_doi_duplicate(
        doi,
        paper_raw_dir=paper_raw_dir,
        papers_dir=papers_dir,
        skip_paper_number=skip_paper_number,
    )
    for ref in dup.refs:
        if ref.scope == "papers":
            reasons.append(f"duplicate_formal_doi: {doi} ({ref.paper_number or ref.paper_name})")
        else:
            reasons.append(f"duplicate_paper_raw_doi: {doi} ({ref.paper_number})")
    return list(dict.fromkeys(reasons))


def _duplicate_pdf_reasons(
    pdf_path: Path,
    *,
    paper_raw_dir: Path,
    papers_dir: Path,
    skip_paper_number: str,
) -> list[str]:
    if not pdf_path.exists():
        return []
    try:
        dup = check_pdf_duplicate(
            pdf_path,
            paper_raw_dir=paper_raw_dir,
            papers_dir=papers_dir,
            skip_paper_number=skip_paper_number,
        )
    except OSError:
        return []
    reasons: list[str] = []
    for ref in dup.refs:
        if ref.pdf_sha256 == dup.pdf_sha256:
            reasons.append(f"duplicate_pdf_sha256: {ref.scope}/{ref.paper_number or ref.paper_name}")
        if ref.pdf_md5 == dup.pdf_md5:
            reasons.append(f"duplicate_pdf_md5: {ref.scope}/{ref.paper_number or ref.paper_name}")
    if "pdf_md5_collision_or_inconsistent_hash" in dup.reasons:
        reasons.append("pdf_md5_collision_or_inconsistent_hash")
    return list(dict.fromkeys(reasons))


# ── Markdown DOI scope ─────────────────────────────────────────────────

def _split_header_and_references(md_text: str) -> tuple[str, str]:
    """Return (header_text, references_text). Header = everything before the
    first References/Bibliography/参考文献 heading."""
    lines = md_text.splitlines()
    boundary = len(lines)
    for idx, line in enumerate(lines):
        if REFERENCES_HEADING_RE.match(line):
            boundary = idx
            break
    header_lines = lines[:min(boundary, MD_HEADER_SCAN_LINES)]
    header_text = "\n".join(header_lines)
    # cap header to the scan window
    if len(header_text) > MD_HEADER_SCAN_CHARS:
        header_text = header_text[:MD_HEADER_SCAN_CHARS]
    references_text = "\n".join(lines[boundary:])
    return header_text, references_text


def _extract_pdf_first_pages_text(pdf_path: Path, max_pages: int = 3) -> str:
    """Best-effort text from the first PDF pages; used only as local evidence."""
    if not pdf_path.exists():
        return ""
    try:
        import fitz
    except ImportError:
        return ""
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return ""
    try:
        chunks: list[str] = []
        for page_num in range(min(max_pages, len(doc))):
            text = doc[page_num].get_text() or ""
            if REFERENCES_HEADING_RE.search(text):
                text = REFERENCES_HEADING_RE.split(text, maxsplit=1)[0]
            chunks.append(text)
        return "\n".join(chunks)
    finally:
        doc.close()


def _clean_local_title_line(line: str) -> str:
    line = re.sub(r"\s+", " ", line.strip())
    line = re.sub(r"^#{1,6}\s*", "", line)
    return line.strip()


def _is_obvious_non_title_line(line: str) -> bool:
    if not line:
        return True
    if re.search(r"10\.\d{4,}/", line, re.IGNORECASE):
        return True
    if re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", line):
        return True
    if re.match(
        r"^(abstract|keywords?|introduction|references?|bibliography|"
        r"copyright|received|accepted|published|doi\b|http|www\.)",
        line,
        re.IGNORECASE,
    ):
        return True
    if re.search(r"(university|institute|department|laboratory|college|school)", line, re.IGNORECASE):
        return True
    return False


def _extract_pdf_title_candidate(pdf_path: Path) -> str:
    """Extract a conservative first-pages title fallback from PDF text."""
    text = _extract_pdf_first_pages_text(pdf_path, max_pages=3)
    for raw in text.splitlines()[:80]:
        line = _clean_local_title_line(raw)
        if len(line) < 16 or _is_obvious_non_title_line(line):
            continue
        if len(line.split()) > 35:
            continue
        return line
    return ""


# ── Local evidence extraction ──────────────────────────────────────────

def _local_evidence(metadata: dict, md_path: Path | None, pdf_path: Path | None = None, *, prefer_markdown: bool = False) -> tuple[str, int | None, str, str, list[str], str, str, str, list[str]]:
    """Return local metadata evidence plus title/author source hints.

    Pulls trusted existing metadata first, then converted Markdown first 100
    physical lines, then PDF first-pages title fallback. Markdown DOI evidence is
    limited to that same header/front-matter region before any References
    heading; reference-list DOIs are not local evidence for this paper.

    When ``prefer_markdown`` is True (post-conversion re-resolution), Markdown
    front-matter title/author evidence is preferred even when existing metadata
    already carries a DOI/title — the converted Markdown is the freshest source.
    DOI priority is unchanged: an existing valid metadata DOI still wins.
    """
    local_title = ((metadata.get("title") or {}).get("original") or "").strip()
    existing_metadata_doi = metadata_doi(metadata)
    title_source = "metadata" if local_title else "none"
    local_year = metadata.get("year")
    local_first_author_family = _surname(first_author_family(metadata))
    author_source = "metadata" if local_first_author_family and local_first_author_family != "unknownauthor" else "none"
    if local_first_author_family == "unknownauthor":
        local_first_author_family = ""
    abstract = metadata.get("abstract") or ""

    md_dois: list[str] = []
    md_front_lines: list[str] = []
    if md_path and md_path.exists():
        try:
            md_text = md_path.read_text(encoding="utf-8")
        except Exception:
            md_text = ""
        header_text, _refs = _split_header_and_references(md_text)
        md_dois = collect_dois_from_text(header_text)
        front = extract_front_matter_candidates_from_markdown(md_path, max_lines=100)
        md_front_lines = list(front.front_matter_lines or [])
        # Default: only fall back to Markdown front-matter when metadata lacks
        # DOI/title. With prefer_markdown (post-convert), always prefer the
        # freshly-converted Markdown evidence over stale metadata.
        prefer_front_title = prefer_markdown or (not existing_metadata_doi or not local_title)
        prefer_front_author = prefer_markdown or (not existing_metadata_doi or not local_first_author_family)
        if front.title_candidates and prefer_front_title:
            local_title = front.title_candidates[0]
            title_source = "markdown_front_matter"
        if front.author_candidates:
            first_line = front.author_candidates[0]
            if first_line and prefer_front_author:
                local_first_author_family = _surname(first_line[0])
                author_source = "markdown_front_matter"
        ext = extract_metadata_from_markdown(md_path, paper_name="", max_scan_chars=MD_HEADER_SCAN_CHARS)
        if local_year is None and ext.year_candidates:
            local_year = ext.year_candidates[0]
        if not abstract:
            abstract = ext.abstract_candidate or abstract
    if not local_title and pdf_path and pdf_path.exists():
        pdf_title = _extract_pdf_title_candidate(pdf_path)
        if pdf_title:
            local_title = pdf_title
            title_source = "pdf_fallback"
    return local_title, local_year, local_first_author_family, abstract, md_dois, "", title_source, author_source, md_front_lines


# ── Candidate construction ─────────────────────────────────────────────

def _candidate_from_enrichment(
    candidate_id: str,
    result: EnrichmentResult,
    doi_source: str,
    *,
    local_title: str,
    local_year: int | None,
    local_first_author_family: str,
    source_id: str,
    evidence_extra: list[str] | None = None,
) -> ResolvedCandidate:
    authoritative = doi_source in AUTHORITATIVE_DOI_SOURCES
    doi_source_conf = 1.0 if authoritative else 0.7
    score, components = score_candidate(
        candidate_title=result.title,
        candidate_year=result.year,
        candidate_authors=result.authors,
        candidate_venue=result.venue,
        local_title=local_title,
        local_year=local_year,
        local_first_author_family=local_first_author_family,
        doi_source_conf=doi_source_conf,
    )
    evidence = [f"doi source: {doi_source}"] + (evidence_extra or [])
    return ResolvedCandidate(
        candidate_id=candidate_id,
        doi=normalize_doi(result.doi),
        title=result.title,
        authors=list(result.authors or []),
        year=result.year,
        venue=result.venue,
        source=result.source or doi_source,
        doi_source=doi_source,
        confidence=float(result.confidence or 0.0),
        score=score,
        score_components=components,
        doi_source_conf=doi_source_conf,
        authoritative=authoritative,
        decision="manual_review",  # finalized after gate
        gate_reasons=[],
        evidence=evidence,
        warnings=list(result.warnings or []),
        patch=patch_from_enrichment(source_id, result),
    )


def _candidate_from_paper(
    candidate_id: str,
    cand: PaperCandidate,
    doi_source: str,
    *,
    local_title: str,
    local_year: int | None,
    local_first_author_family: str,
    source_id: str,
    resolvable: bool,
    evidence_extra: list[str] | None = None,
) -> ResolvedCandidate:
    authoritative = doi_source in AUTHORITATIVE_DOI_SOURCES
    doi_source_conf = 1.0 if authoritative else 0.7
    score, components = score_candidate(
        candidate_title=cand.title,
        candidate_year=cand.year,
        candidate_authors=cand.authors,
        candidate_venue=cand.venue,
        local_title=local_title,
        local_year=local_year,
        local_first_author_family=local_first_author_family,
        doi_source_conf=doi_source_conf,
    )
    evidence = [f"doi source: {doi_source}", f"network search: {cand.source}"] + (evidence_extra or [])
    if not resolvable:
        evidence.append("doi NOT resolvable by Crossref")
    return ResolvedCandidate(
        candidate_id=candidate_id,
        doi=normalize_doi(cand.doi),
        title=cand.title,
        authors=list(cand.authors or []),
        year=cand.year,
        venue=cand.venue,
        source=cand.source or doi_source,
        doi_source=doi_source,
        confidence=float(cand.confidence or 0.0),
        score=score,
        score_components=components,
        doi_source_conf=doi_source_conf,
        authoritative=authoritative,
        decision="manual_review",
        gate_reasons=[],
        evidence=evidence,
        warnings=[],
        patch=patch_from_candidate(source_id, cand),
    )


def _finalize_decisions(
    candidates: list[ResolvedCandidate],
    *,
    local_title: str,
    local_year: int | None,
    local_first_author_family: str,
    existing_doi: str,
    paper_raw_dir: Path,
    papers_dir: Path,
    source_id: str,
    duplicate_pdf_reasons: list[str],
    min_confidence: float,
) -> None:
    """Set gate_reasons + decision on each candidate in place.

    Decision logic:
    - authoritative candidate that PASSES the gate → auto_matched (the gate already
      enforces DOI validity/resolvability, local-evidence consistency or authoritative
      completeness, no conflict, no duplicate). Score does NOT gate auto-match here;
      score only ranks candidates and sorts manual_review vs rejected.
    - otherwise (gate fails, or network title-search): manual_review if it has a DOI
      and score >= min_confidence, else rejected. Network-title candidates never
      auto-match.
    """
    for c in candidates:
        if not c.doi:
            c.gate_reasons = ["no doi"]
            c.decision = "rejected"
            continue
        duplicate_reasons = [
            *_duplicate_candidate_reasons(
                c.doi,
                paper_raw_dir=paper_raw_dir,
                papers_dir=papers_dir,
                skip_paper_number=source_id,
            ),
            *duplicate_pdf_reasons,
        ]
        if c.authoritative:
            auth_complete = bool(
                c.title and c.year is not None and c.authors and c.venue and c.doi
            )
            passes, reasons = auto_match_gate(
                doi=c.doi,
                doi_source=c.doi_source,
                resolvable=True,  # authoritative candidates were Crossref-resolved
                candidate_title=c.title,
                candidate_year=c.year,
                candidate_authors=c.authors,
                candidate_venue=c.venue,
                local_title=local_title,
                local_year=local_year,
                local_first_author_family=local_first_author_family,
                existing_doi=existing_doi,
                authoritative_complete=auth_complete,
            )
            c.gate_reasons = list(dict.fromkeys([*reasons, *duplicate_reasons]))
            if duplicate_reasons:
                c.decision = "rejected"
            elif passes:
                c.decision = "auto_matched"
            elif c.score >= min_confidence:
                c.decision = "manual_review"
            else:
                c.decision = "rejected"
        else:
            # network title-search: never auto_matched
            c.gate_reasons = list(dict.fromkeys(["network title-search candidate: never auto-matched", *duplicate_reasons]))
            c.decision = "rejected" if duplicate_reasons else (
                "manual_review" if (c.doi and c.score >= min_confidence) else "rejected"
            )


# ── Orchestrator ───────────────────────────────────────────────────────

def resolve_metadata_candidates(
    folder: str | Path,
    *,
    allow_network: bool = True,
    max_candidates: int = 5,
    min_confidence: float = MANUAL_REVIEW_THRESHOLD,
    papers_dir: str | Path = PAPERS_DIR,
    paper_raw_dir: str | Path | None = None,
    prefer_markdown: bool = False,
    rate_limiter=None,
) -> ResolveReport:
    """Resolve metadata candidates for a paper_raw folder. Does NOT write files.

    ``prefer_markdown`` (the ``--prefer-markdown`` / post-conversion signal)
    is mirrored onto the returned report as ``post_conversion`` so callers and
    reports can tell this was a post-conversion re-resolution pass.

    ``rate_limiter`` (optional ``ProviderRateLimiter``) enables conservative
    spacing + 429/403/timeout backoff for network calls. When ``None`` and
    ``allow_network`` is ``True``, a ``ValueError`` is raised — network
    access without rate limiting is not permitted. Callers that intentionally
    test the fallback path (e.g. with mocked HTTP) must pass a
    ``ProviderRateLimiter`` with zero intervals.
    """
    if allow_network and rate_limiter is None:
        raise ValueError(
            "allow_network=True requires a ProviderRateLimiter. "
            "Create one with ProviderRateLimiter(default_config()) or use "
            "the canonical CLI which builds one automatically."
        )
    report = _resolve_metadata_candidates_impl(
        folder,
        allow_network=allow_network,
        max_candidates=max_candidates,
        min_confidence=min_confidence,
        papers_dir=papers_dir,
        paper_raw_dir=paper_raw_dir,
        prefer_markdown=prefer_markdown,
        rate_limiter=rate_limiter,
    )
    report.post_conversion = prefer_markdown
    return report


def _resolve_metadata_candidates_impl(
    folder: str | Path,
    *,
    allow_network: bool = True,
    max_candidates: int = 5,
    min_confidence: float = MANUAL_REVIEW_THRESHOLD,
    papers_dir: str | Path = PAPERS_DIR,
    paper_raw_dir: str | Path | None = None,
    prefer_markdown: bool = False,
    rate_limiter=None,
) -> ResolveReport:
    folder = Path(folder)
    source_id = folder.name
    meta_path = folder / f"{source_id}.metadata.json"
    from src.metadata.freeze import assert_metadata_write_allowed
    assert_metadata_write_allowed(folder, source_id)
    pdf_path = folder / f"{source_id}.pdf"
    md_path = folder / f"{source_id}.md"
    paper_raw_root = Path(paper_raw_dir) if paper_raw_dir is not None else folder.parent
    papers_root = Path(papers_dir)

    if not meta_path.exists():
        raise FileNotFoundError(f"metadata file missing: {meta_path}")
    metadata = _read_json(meta_path, {})
    existing_doi = metadata_doi(metadata)

    pdf_sha256 = ""
    if pdf_path.exists():
        try:
            pdf_sha256 = compute_sha256(pdf_path)
        except Exception:
            pdf_sha256 = ""

    (
        local_title,
        local_year,
        local_first_author_family,
        _abstract,
        md_header_dois,
        _,
        title_source,
        author_source,
        markdown_front_matter_lines,
    ) = _local_evidence(metadata, md_path if md_path.exists() else None, pdf_path if pdf_path.exists() else None, prefer_markdown=prefer_markdown)
    local_title_evidence_missing = not bool(local_title)
    local_doi_candidates = [existing_doi] if existing_doi else list(md_header_dois)

    duplicate_pdf_reasons = _duplicate_pdf_reasons(
        pdf_path,
        paper_raw_dir=paper_raw_root,
        papers_dir=papers_root,
        skip_paper_number=source_id,
    )

    candidates: list[ResolvedCandidate] = []
    warnings: list[str] = []
    doi_source = "none"
    decision = "no_candidates"
    reason = ""

    cid = 0

    def _next_id() -> str:
        nonlocal cid
        cid += 1
        return f"cand_{cid:03d}"

    def _enrich(doi: str) -> EnrichmentResult:
        """Enrich from DOI.

        When ``allow_network=False`` the enrichment is purely local (no HTTP).
        When ``allow_network=True`` a ``rate_limiter`` is required; the call
        goes through ``rate_limiter.pace_paper(provider)`` for paper-level
        pacing (the ProviderClient layer owns the per-request min interval)
        and then delegates to the monkeypatchable ``enrich_from_doi``.
        """
        if not allow_network:
            return enrich_from_doi(doi, query_crossref=False)
        if rate_limiter is None:
            raise ValueError("allow_network=True requires a ProviderRateLimiter")
        rate_limiter.pace_paper("crossref")
        return enrich_from_doi(doi, query_crossref=True)

    def _title_search_crossref(title: str, year, limit: int) -> list[PaperCandidate]:
        if rate_limiter is None:
            raise ValueError("allow_network=True requires a ProviderRateLimiter")
        rate_limiter.pace_paper("crossref")
        try:
            return resolve_crossref_by_title(title, year=year, limit=limit)
        except ProviderRateLimited:
            # 429 from the unified ProviderClient: preserve the legacy
            # "network error -> empty list" contract for the metadata resolver.
            return []

    def _title_search_openalex(title: str, limit: int) -> list[PaperCandidate]:
        if rate_limiter is None:
            raise ValueError("allow_network=True requires a ProviderRateLimiter")
        rate_limiter.pace_paper("openalex")
        return search_openalex(title, limit=limit)

    def _crossref_doi_resolvable(doi: str) -> bool:
        if rate_limiter is None:
            raise ValueError("allow_network=True requires a ProviderRateLimiter")
        rate_limiter.pace_paper("crossref")
        return get_crossref_work_by_doi(doi) is not None

    # ── Branch 1: existing metadata DOI ──
    if existing_doi:
        doi_source = "metadata"
        try:
            result = _enrich(existing_doi)
        except Exception as exc:
            result = EnrichmentResult(doi=existing_doi, warnings=[f"enrichment error: {exc}"])
        result_doi = normalize_doi(getattr(result, "doi", ""))
        if result_doi and result_doi != normalize_doi(existing_doi):
            warnings.append(f"DOI conflict: metadata {existing_doi} vs Crossref {result_doi}")
            decision = "conflict"
            reason = f"existing DOI {existing_doi} conflicts with Crossref-returned {result_doi}"
            return ResolveReport(
                source_id=source_id, folder=str(folder), metadata_path=str(meta_path),
                existing_doi=existing_doi, doi_source="conflict",
                local_title=local_title, local_year=local_year,
                local_first_author_family=local_first_author_family, pdf_sha256=pdf_sha256,
                candidates=[], best_candidate_id=None, decision=decision, reason=reason,
                warnings=warnings, created_at=_now_iso(), applied=False, applied_status="",
                title_source=title_source,
                author_source=author_source,
                markdown_front_matter_lines=markdown_front_matter_lines,
                local_title_evidence_missing=local_title_evidence_missing,
                local_doi_candidates=local_doi_candidates,
            )
        if not result_doi:
            warnings.append("existing DOI not resolvable by Crossref")
            decision = "manual_review"
            reason = "existing DOI not resolvable by Crossref; manual review required"
        else:
            cand = _candidate_from_enrichment(
                _next_id(), result, doi_source="metadata",
                local_title=local_title, local_year=local_year,
                local_first_author_family=local_first_author_family, source_id=source_id,
                evidence_extra=[f"existing metadata doi: {existing_doi}"],
            )
            candidates.append(cand)
    else:
        # ── Branch 2: DOI from filename / pdf / markdown ──
        found_dois: list[tuple[str, str]] = []  # (doi, source)
        fn_doi = extract_doi_from_filename(pdf_path.name) if pdf_path.exists() else None
        if fn_doi:
            found_dois.append((normalize_doi(fn_doi), "filename"))
        pdf_doi = None
        try:
            pdf_doi = extract_doi_from_pdf_file(pdf_path) if pdf_path.exists() else None
        except Exception:
            pdf_doi = None
        if pdf_doi:
            n = normalize_doi(pdf_doi)
            if not any(d == n for d, _ in found_dois):
                found_dois.append((n, "pdf"))
        for d in md_header_dois:
            if not any(dd == d for dd, _ in found_dois):
                found_dois.append((d, "markdown"))
        local_doi_candidates = list(dict.fromkeys(d for d, _ in found_dois if d))

        distinct_dois = list({d for d, _ in found_dois})
        if len(distinct_dois) >= 2:
            warnings.append(
                "multiple distinct DOIs found in filename/pdf/markdown: "
                + ", ".join(f"{d} ({src})" for d, src in found_dois)
            )
            decision = "conflict"
            reason = "multiple distinct DOIs; disambiguation requires manual review"
            return ResolveReport(
                source_id=source_id, folder=str(folder), metadata_path=str(meta_path),
                existing_doi=existing_doi, doi_source="conflict",
                local_title=local_title, local_year=local_year,
                local_first_author_family=local_first_author_family, pdf_sha256=pdf_sha256,
                candidates=[], best_candidate_id=None, decision=decision, reason=reason,
                warnings=warnings, created_at=_now_iso(), applied=False, applied_status="",
                title_source=title_source,
                author_source=author_source,
                markdown_front_matter_lines=markdown_front_matter_lines,
                local_title_evidence_missing=local_title_evidence_missing,
                local_doi_candidates=local_doi_candidates,
            )
        if len(distinct_dois) == 1:
            doi = distinct_dois[0]
            doi_source = next(src for d, src in found_dois if d == doi)
            try:
                result = _enrich(doi)
            except Exception as exc:
                result = EnrichmentResult(doi=doi, warnings=[f"enrichment error: {exc}"])
            if not normalize_doi(getattr(result, "doi", "")):
                warnings.append(f"DOI {doi} from {doi_source} not resolvable by Crossref")
                decision = "manual_review"
                reason = f"DOI {doi} from {doi_source} not resolvable; manual review required"
            else:
                cand = _candidate_from_enrichment(
                    _next_id(), result, doi_source=doi_source,
                    local_title=local_title, local_year=local_year,
                    local_first_author_family=local_first_author_family, source_id=source_id,
                    evidence_extra=[f"doi extracted from {doi_source}"],
                )
                candidates.append(cand)
        else:
            # ── Branch 3: no DOI anywhere → network title search ──
            if not allow_network:
                decision = "no_candidates"
                reason = "no DOI in metadata/filename/pdf/markdown and network disabled"
            elif not local_title:
                decision = "no_candidates"
                reason = "no DOI and no title candidate for network search"
            else:
                doi_source = "network_title"
                net_cands: list[PaperCandidate] = []
                try:
                    net_cands.extend(_title_search_crossref(local_title, local_year, max_candidates))
                except Exception as exc:
                    warnings.append(f"crossref title search failed: {exc}")
                if len(net_cands) < max_candidates:
                    try:
                        net_cands.extend(_title_search_openalex(local_title, max_candidates))
                    except Exception as exc:
                        warnings.append(f"openalex search failed: {exc}")
                # keep only DOI-bearing, dedupe by doi
                seen_dois: set[str] = set()
                for cand in net_cands:
                    nd = normalize_doi(cand.doi)
                    if not nd or "/" not in nd or nd in seen_dois:
                        continue
                    seen_dois.add(nd)
                    resolvable = False
                    try:
                        resolvable = _crossref_doi_resolvable(nd)
                    except Exception:
                        resolvable = False
                    rc = _candidate_from_paper(
                        _next_id(), cand, doi_source="network_title",
                        local_title=local_title, local_year=local_year,
                        local_first_author_family=local_first_author_family, source_id=source_id,
                        resolvable=resolvable,
                    )
                    candidates.append(rc)
                    if len(candidates) >= max_candidates:
                        break

    # ── Finalize decisions ──
    _finalize_decisions(
        candidates,
        local_title=local_title, local_year=local_year,
        local_first_author_family=local_first_author_family,
        existing_doi=existing_doi,
        paper_raw_dir=paper_raw_root, papers_dir=papers_root, source_id=source_id,
        duplicate_pdf_reasons=duplicate_pdf_reasons,
        min_confidence=min_confidence,
    )

    # pick best: prefer auto_matched, then highest score
    best: ResolvedCandidate | None = None
    for c in candidates:
        if c.decision == "rejected":
            continue
        if best is None or c.score > best.score or (c.score == best.score and c.authoritative and not best.authoritative):
            best = c
    best_id = best.candidate_id if best else None

    if not candidates:
        decision = "no_candidates"
        reason = reason or "no metadata candidates found"
    elif best is None:
        decision = "rejected"
        reason = "all candidates rejected"
    elif best.decision == "auto_matched":
        decision = "auto_matched"
        reason = f"best candidate {best.candidate_id} (doi {best.doi}) passed auto-match gate"
    else:
        decision = "manual_review"
        reason = f"best candidate {best.candidate_id} requires manual confirmation; gate: {best.gate_reasons}"

    return ResolveReport(
        source_id=source_id, folder=str(folder), metadata_path=str(meta_path),
        existing_doi=existing_doi, doi_source=doi_source,
        local_title=local_title, local_year=local_year,
        local_first_author_family=local_first_author_family, pdf_sha256=pdf_sha256,
        candidates=candidates, best_candidate_id=best_id, decision=decision, reason=reason,
        warnings=warnings, created_at=_now_iso(), applied=False, applied_status="",
        title_source=title_source,
        author_source=author_source,
        markdown_front_matter_lines=markdown_front_matter_lines,
        local_title_evidence_missing=local_title_evidence_missing,
        local_doi_candidates=local_doi_candidates,
    )


# ── Apply ──────────────────────────────────────────────────────────────

def _has_required_metadata_fields(metadata: dict) -> bool:
    doi = ((metadata.get("identifiers") or {}).get("doi") or "").strip()
    title = ((metadata.get("title") or {}).get("original") or "").strip()
    year = metadata.get("year")
    authors = metadata.get("authors") or []
    has_author = any((a.get("full_name") or a.get("family")) for a in authors if isinstance(a, dict))
    return bool(doi and title and year and has_author)


def _has_venue(metadata: dict) -> bool:
    container = metadata.get("container") or {}
    return any(str(container.get(k) or "").strip() for k in ("journal", "conference", "booktitle"))


def apply_resolution(
    folder: str | Path,
    report: ResolveReport,
    *,
    manual_confirm: bool = False,
    candidate_id: str | None = None,
    papers_dir: str | Path = PAPERS_DIR,
    paper_raw_dir: str | Path | None = None,
) -> dict:
    folder_path = Path(folder)
    paper_raw_root = Path(paper_raw_dir) if paper_raw_dir is not None else folder_path.parent
    with paper_raw_write_lock(paper_raw_root):
        return _apply_resolution_unlocked(
            folder_path,
            report,
            manual_confirm=manual_confirm,
            candidate_id=candidate_id,
            papers_dir=papers_dir,
            paper_raw_dir=paper_raw_root,
        )


def _apply_resolution_unlocked(
    folder: str | Path,
    report: ResolveReport,
    *,
    manual_confirm: bool = False,
    candidate_id: str | None = None,
    papers_dir: str | Path = PAPERS_DIR,
    paper_raw_dir: str | Path | None = None,
) -> dict:
    """Apply a resolved candidate to metadata.json. Returns a result dict.

    - candidate selection writes citation metadata once but does not assert a
      PDF match; the independent receipt stage owns that decision.
    - --manual-confirm selects a candidate only after passing the full
      DOI/dupe(DOI+sha)/conflict/completeness/no-overwrite gate. It relaxes ONLY
      the auto-score threshold, never the validation checks.
    """
    folder = Path(folder)
    source_id = folder.name
    meta_path = folder / f"{source_id}.metadata.json"
    metadata = _read_json(meta_path, {})
    paper_raw_root = Path(paper_raw_dir) if paper_raw_dir is not None else folder.parent
    papers_root = Path(papers_dir)

    # choose candidate
    chosen: ResolvedCandidate | None = None
    if candidate_id:
        for c in report.candidates:
            if c.candidate_id == candidate_id:
                chosen = c
                break
        if chosen is None:
            raise ValueError(f"candidate_id {candidate_id!r} not found among report candidates")
    else:
        for c in report.candidates:
            if c.candidate_id == report.best_candidate_id:
                chosen = c
                break
    if chosen is None or not chosen.doi:
        candidate_warnings = list(dict.fromkeys(
            reason
            for candidate in report.candidates
            for reason in candidate.gate_reasons
        ))
        write_import_status(folder, STATUS_MANUAL_REVIEW, reason="no DOI-bearing candidate to apply")
        return {"applied": False, "status": "no_candidate", "paper_number": source_id, "paper_raw_id": source_id,
                "chosen_candidate_id": candidate_id or report.best_candidate_id,
                "warnings": candidate_warnings or ["no DOI-bearing candidate"]}

    existing_doi = metadata_doi(metadata)

    # ── Full validation gate (applies to BOTH auto and manual-confirm) ──
    fail_reasons: list[str] = []
    if "/" not in chosen.doi:
        fail_reasons.append("doi malformed")
    fail_reasons.extend(_duplicate_candidate_reasons(
        chosen.doi,
        paper_raw_dir=paper_raw_root,
        papers_dir=papers_root,
        skip_paper_number=source_id,
    ))
    fail_reasons.extend(_duplicate_pdf_reasons(
        folder / f"{source_id}.pdf",
        paper_raw_dir=paper_raw_root,
        papers_dir=papers_root,
        skip_paper_number=source_id,
    ))
    if existing_doi and normalize_doi(existing_doi) != normalize_doi(chosen.doi):
        fail_reasons.append(f"doi conflict: existing {existing_doi} vs candidate {chosen.doi}")

    # merge first (fills only empties) so we can check completeness on merged data
    merged, merge_warnings = merge_missing_metadata(metadata, chosen.patch)
    gate_ready, gate_reasons = bibliographic_identity_gate(merged, fail_reasons)
    fail_reasons = [] if gate_ready else gate_reasons
    can_auto = chosen.decision == "auto_matched"
    if fail_reasons or (not can_auto and not manual_confirm):
        status = STATUS_MANUAL_REVIEW if chosen.doi else STATUS_RESOLVE_FAILED
        reason = "; ".join(fail_reasons) if fail_reasons else (
            "candidate not auto-matched and --manual-confirm not given"
        )
        write_import_status(folder, status, reason=reason)
        return {"applied": False, "status": "manual_review_required", "paper_number": source_id, "paper_raw_id": source_id,
                "chosen_candidate_id": chosen.candidate_id, "warnings": fail_reasons or [reason]}

    # ── Write ──
    new_status = "resolved"
    merged.pop("metadata_match", None)
    schema_errors = validate_metadata_schema(merged)
    if schema_errors:
        write_import_status(folder, STATUS_MANUAL_REVIEW, reason="; ".join(schema_errors))
        return {"applied": False, "status": "schema_error", "paper_number": source_id, "paper_raw_id": source_id,
                "chosen_candidate_id": chosen.candidate_id, "warnings": schema_errors}

    source = merged.get("source") if isinstance(merged.get("source"), dict) else {}
    provider = str(source.get("provider") or chosen.source or "metadata_resolution")
    # raw_record_path must always point at a metadata source record, never at
    # fetch_result.json. Use source_records/metadata_source.<provider>.json.
    from src.metadata.source_records import ensure_raw_record_path_is_metadata_source
    raw_record_path = ensure_raw_record_path_is_metadata_source(
        source.get("raw_record_path") or "", provider,
    )
    source["raw_record_path"] = raw_record_path
    merged["source"] = source
    write_metadata_source_record(folder, provider, chosen.to_dict())
    atomic_write_json(meta_path, merged, indent=2)
    write_import_status(folder, "metadata_resolved", reason=f"citation metadata selected from candidate {chosen.candidate_id}; PDF match pending")

    report.applied = True
    report.applied_status = new_status
    report.chosen_candidate_id = chosen.candidate_id

    return {"applied": True, "status": new_status, "paper_number": source_id, "paper_raw_id": source_id,
            "chosen_candidate_id": chosen.candidate_id, "doi": chosen.doi, "warnings": merge_warnings}


# ── Side-file writers ──────────────────────────────────────────────────

def _compact_patch(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            if key in {"metadata_match", "bibtex", "pdf", "content", "notes", "abstract", "keywords"}:
                continue
            if key in {"short_zh", "translated_zh", "raw_record"}:
                continue
            compacted = _compact_patch(child)
            if compacted not in ("", None, [], {}):
                out[key] = compacted
        return out
    if isinstance(value, list):
        out = [_compact_patch(item) for item in value]
        return [item for item in out if item not in ("", None, [], {})]
    if isinstance(value, str):
        return value.strip()
    return value


def write_metadata_patch_json(folder: Path, report: ResolveReport) -> Path | None:
    if not report.best_candidate_id:
        return None
    chosen = next((c for c in report.candidates if c.candidate_id == report.best_candidate_id), None)
    if chosen is None or chosen.decision == "rejected":
        return None
    allowed_top_level = {
        "warnings",
        "title",
        "authors",
        "first_author",
        "year",
        "container",
        "publication",
        "identifiers",
        "links",
        "source",
    }
    patch = {
        key: value
        for key, value in _compact_patch(chosen.patch).items()
        if key in allowed_top_level
    }
    if not patch:
        return None
    path = folder / f"{report.source_id}.metadata.patch.json"
    atomic_write_json(path, patch, indent=2)
    return path


def write_candidates_json(folder: Path, report: ResolveReport) -> Path:
    path = folder / f"{report.source_id}.metadata.candidates.json"
    data = {
        "paper_number": report.source_id,
        "paper_raw_id": report.source_id,
        "generated_at": report.created_at,
        "candidates": [
            {
                "candidate_id": c.candidate_id,
                "doi": c.doi,
                "title": c.title,
                "authors": c.authors,
                "year": c.year,
                "venue": c.venue,
                "source": c.source,
                "confidence": c.confidence,
                "score": c.score,
                "evidence": c.evidence,
                "warnings": c.warnings,
            }
            for c in report.candidates
        ],
        "recommendation": {
            "best_candidate_id": report.best_candidate_id,
            "decision": report.decision,
            "reason": report.reason,
        },
    }
    atomic_write_json(path, data, indent=2)
    return path


def write_resolve_report_json(folder: Path, report: ResolveReport) -> Path:
    path = folder / f"{report.source_id}.metadata.resolve_report.json"
    atomic_write_json(path, report.to_dict(), indent=2)
    return path
