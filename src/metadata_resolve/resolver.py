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
- ``src.metadata_resolve.enrichment`` (DOI extraction + Crossref enrichment)
- ``src.metadata_resolve.markdown_extract`` (Markdown candidate extraction)
- ``src.metadata.schema`` / ``src.metadata.normalization``
"""
from __future__ import annotations

from pathlib import Path

from config.settings import PAPERS_DIR
from src.discovery.models import PaperCandidate
from src.discovery.providers.provider_errors import ProviderRateLimited
from src.discovery.resolve_crossref import (
    get_crossref_work_by_doi,
    resolve_crossref_by_title,
)
from src.discovery.search_openalex import search_openalex
from src.utils.file_fingerprint import compute_sha256
from src.metadata.schema import metadata_doi
from src.utils.identifiers import normalize_doi
from src.utils.jsonio import read_json
from src.utils.timestamps import now_iso as _now_iso
from src.metadata_resolve.candidates import (
    ResolvedCandidate,
    ResolveReport,
    _candidate_from_enrichment,
    _candidate_from_paper,
    _duplicate_pdf_reasons,
    _finalize_decisions,
)
from src.metadata_resolve.enrichment import (
    EnrichmentResult,
    enrich_from_doi,
    extract_doi_from_filename,
    extract_doi_from_pdf_file,
)
from src.metadata_resolve.evidence import local_evidence
from src.metadata_resolve.scoring import MANUAL_REVIEW_THRESHOLD


_read_json = read_json


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
    ) = local_evidence(metadata, md_path if md_path.exists() else None, pdf_path if pdf_path.exists() else None, prefer_markdown=prefer_markdown)
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
