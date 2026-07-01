"""Candidate normalization, dedupe, and ranking."""
from difflib import SequenceMatcher

from src.discovery.models import PaperCandidate, normalize_doi, normalize_title


def _candidate_key(candidate: PaperCandidate) -> tuple[str, str]:
    doi = normalize_doi(candidate.doi)
    if doi:
        return ("doi", doi)
    return ("title", f"{normalize_title(candidate.title)}:{candidate.year or ''}")


def _source_list(candidate: PaperCandidate) -> list[str]:
    raw_sources = candidate.raw.get("sources") if isinstance(candidate.raw, dict) else None
    sources = list(raw_sources or [])
    if candidate.source:
        sources.extend([s.strip() for s in candidate.source.split(",") if s.strip()])
    return sorted(set(sources))


def _merge(a: PaperCandidate, b: PaperCandidate) -> PaperCandidate:
    sources = sorted(set(_source_list(a) + _source_list(b)))
    raw = dict(a.raw or {})
    raw["sources"] = sources
    raw.setdefault("merged_raw", [])
    raw["merged_raw"].append({"source": b.source, "provider_id": b.source_id, "raw": b.raw})
    return PaperCandidate(
        title=a.title or b.title,
        year=a.year or b.year,
        authors=a.authors or b.authors,
        doi=a.doi or b.doi,
        venue=a.venue or b.venue,
        abstract=a.abstract or b.abstract,
        source=",".join(sources),
        source_id=a.source_id or b.source_id,
        url=a.url or b.url,
        pdf_url=a.pdf_url or b.pdf_url,
        open_access=bool(a.open_access or b.open_access),
        citation_count=max([x for x in [a.citation_count, b.citation_count] if x is not None], default=None),
        confidence=max(a.confidence, b.confidence),
        query=a.query or b.query,
        domain_id=a.domain_id or b.domain_id,
        raw=raw,
    )


def score_candidate(candidate: PaperCandidate, query: str = "") -> float:
    score = 0.0
    if candidate.doi:
        score += 0.4
    if len(_source_list(candidate)) > 1:
        score += 0.2
    if candidate.open_access or candidate.pdf_url:
        score += 0.2
    if query and candidate.title:
        score += 0.15 * SequenceMatcher(None, normalize_title(query), normalize_title(candidate.title)).ratio()
    if candidate.citation_count:
        score += min(0.1, candidate.citation_count / 1000.0)
    return max(candidate.confidence, min(score, 1.0))


def dedupe_and_rank_candidates(
    candidates: list[PaperCandidate],
    query: str = "",
    max_candidates: int = 50,
) -> list[PaperCandidate]:
    merged: dict[tuple[str, str], PaperCandidate] = {}
    for candidate in candidates:
        key = _candidate_key(candidate)
        if not key[1]:
            continue
        if key in merged:
            merged[key] = _merge(merged[key], candidate)
        else:
            sources = _source_list(candidate)
            raw = dict(candidate.raw or {})
            raw["sources"] = sources
            candidate.raw = raw
            candidate.source = ",".join(sources)
            merged[key] = candidate

    ranked = []
    for candidate in merged.values():
        candidate.confidence = score_candidate(candidate, query=query)
        ranked.append(candidate)
    ranked.sort(key=lambda c: (c.confidence, c.citation_count or 0, c.year or 0), reverse=True)
    return ranked[:max_candidates]
