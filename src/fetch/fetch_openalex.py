"""OpenAlex DOI lookup for OA PDF locations.

The works lookup goes through the unified ``ProviderClient`` (shared
limiter, retry/backoff, circuit breaker, proxy-configured transport);
only the PDF binary download itself uses ``pdf_transport``.
"""
from loguru import logger

from src.utils.identifiers import normalize_doi
from src.discovery.providers.provider_client import ProviderRuntime, RequestSpec
from src.fetch.models import FetchResult
from src.fetch.openalex_credentials import (
    load_openalex_credentials,
    safe_request_error_summary,
)


OPENALEX_WORKS_URL = "https://api.openalex.org/works"


def resolve_openalex_pdf(doi: str) -> FetchResult:
    normalized = normalize_doi(doi)
    params: dict[str, str] = {"filter": f"doi:https://doi.org/{normalized}", "per-page": "1"}
    headers: dict[str, str] = {}
    credentials = load_openalex_credentials()
    if credentials.email:
        params["mailto"] = credentials.email
    if credentials.api_key:
        headers["Authorization"] = f"Bearer {credentials.api_key}"
    spec = RequestSpec(
        provider="openalex",
        purpose="metadata_resolution",
        url=OPENALEX_WORKS_URL,
        params=params,
        headers=headers,
        timeout_seconds=20,
    )
    try:
        outcome = ProviderRuntime.get().client("openalex").execute(spec)
        data = outcome.json()
    except Exception as exc:
        safe_error = safe_request_error_summary(exc)
        logger.warning("OpenAlex DOI lookup failed for {!r}: {}", doi, safe_error)
        return FetchResult(doi=doi, source="openalex", error=safe_error)

    results = data.get("results") or []
    if not results:
        return FetchResult(doi=doi, source="openalex", error="not found", metadata=data)
    work = results[0]
    oa = work.get("open_access") or {}
    primary = work.get("primary_location") or {}
    pdf_url = primary.get("pdf_url")
    is_landing_fallback = False
    if not pdf_url:
        # fallback 到 oa_url（可能是 landing page），标记以便下载阶段校验
        pdf_url = oa.get("oa_url") or ""
        is_landing_fallback = bool(pdf_url)
    if not (oa.get("is_oa") and pdf_url):
        return FetchResult(doi=doi, source="openalex", oa_status=oa.get("oa_status") or "", metadata=work, error="no OA PDF URL")
    meta = dict(work)
    if is_landing_fallback:
        meta["maybe_landing_page"] = True
    return FetchResult(
        doi=doi,
        success=True,
        source="openalex",
        candidate_url=OPENALEX_WORKS_URL,
        pdf_url=pdf_url,
        oa_status=oa.get("oa_status") or "oa",
        metadata=meta,
    )
