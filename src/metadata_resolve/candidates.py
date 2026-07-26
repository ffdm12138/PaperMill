"""Candidate/report dataclasses, patch and candidate builders, duplicate checks.

The resolver emits pure bibliographic candidates only; patches are nested
``empty_metadata`` subsets. Decisions are finalized in place by
``_finalize_decisions`` using the auto-match gate plus duplicate-reason
helpers over ``paper_raw`` and active ``papers``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from src.discovery.models import PaperCandidate
from src.ingest.duplicate_guard import check_doi_duplicate, check_pdf_duplicate
from src.metadata.schema import empty_metadata
from src.utils.identifiers import normalize_doi
from src.utils.timestamps import now_iso as _now_iso
from src.metadata_resolve.enrichment import EnrichmentResult
from src.metadata_resolve.names import split_name as _split_name
from src.metadata_resolve.scoring import (
    AUTHORITATIVE_DOI_SOURCES,
    auto_match_gate,
    score_candidate,
)


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


# ── Duplicate-reason helpers ───────────────────────────────────────────

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
