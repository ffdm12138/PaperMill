"""Semantic Scholar DOI lookup for OA PDF locations.

The lookup goes through the unified ``ProviderClient`` (shared limiter,
retry/backoff honoring ``Retry-After``, circuit breaker).  The unauthenticated
Semantic Scholar pool is shared across all callers and is exhausted instantly
by unpaced requests, so pacing is a correctness requirement here, not a
politeness one.
"""
import os
from urllib.parse import quote

from loguru import logger

from src.discovery.providers.provider_client import ProviderRuntime, RequestSpec
from src.fetch.models import FetchResult
from src.fetch.oa_locations import candidates_from_semantic_scholar


S2_PAPER_URL = "https://api.semanticscholar.org/graph/v1/paper"
S2_FIELDS = "title,year,externalIds,url,openAccessPdf,isOpenAccess"
S2_PROVIDER = "semantic_scholar"


def _headers() -> dict[str, str]:
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    return {"x-api-key": api_key} if api_key else {}


def resolve_semantic_scholar_pdf(doi: str) -> FetchResult:
    candidate_url = f"{S2_PAPER_URL}/DOI:{quote(doi, safe='')}"
    spec = RequestSpec(
        provider=S2_PROVIDER,
        purpose="metadata_resolution",
        url=candidate_url,
        params={"fields": S2_FIELDS},
        headers=_headers(),
        timeout_seconds=20,
    )
    try:
        data = ProviderRuntime.get().client(S2_PROVIDER).execute(spec).json()
    except Exception as exc:
        logger.warning("Semantic Scholar DOI lookup failed for {!r}: {}", doi, type(exc).__name__)
        return FetchResult(doi=doi, source="semantic_scholar", error=str(exc))

    candidates = candidates_from_semantic_scholar(data)
    if not (data.get("isOpenAccess") and candidates):
        return FetchResult(doi=doi, source="semantic_scholar", metadata=data, error="no OA PDF URL")
    best = candidates[0]
    return FetchResult(
        doi=doi,
        success=True,
        source="semantic_scholar",
        candidate_url=candidate_url,
        pdf_url=best.url,
        pdf_candidates=[candidate.to_dict() for candidate in candidates],
        oa_status="oa",
        license=best.license,
        metadata=data,
    )
