"""Candidate scoring and the auto-match gate for metadata resolution.

Score only ranks candidates / assigns the auto/manual band. It is NEVER the
source of a metadata fact. Network-title candidates never pass the gate.
"""
from __future__ import annotations

from difflib import SequenceMatcher

from src.discovery.models import PaperCandidate
from src.utils.identifiers import normalize_doi, normalize_title
from src.metadata_resolve.enrichment import EnrichmentResult
from src.metadata_resolve.names import surname as _surname


AUTHORITATIVE_DOI_SOURCES = {"filename", "pdf", "markdown", "metadata"}
NETWORK_DOI_SOURCES = {"network_title"}

# Decision bands
AUTO_MATCH_THRESHOLD = 0.88
MANUAL_REVIEW_THRESHOLD = 0.70


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
