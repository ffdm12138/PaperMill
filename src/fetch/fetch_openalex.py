"""OpenAlex DOI lookup for OA PDF locations."""
import os

import requests
from loguru import logger

from src.discovery.models import normalize_doi
from src.fetch.models import FetchResult
from src.fetch.proxy import get_fetch_proxies


OPENALEX_WORKS_URL = "https://api.openalex.org/works"


def resolve_openalex_pdf(doi: str) -> FetchResult:
    normalized = normalize_doi(doi)
    params: dict[str, str] = {"filter": f"doi:https://doi.org/{normalized}", "per-page": "1"}
    email = os.environ.get("OPENALEX_EMAIL", "").strip()
    if email:
        params["mailto"] = email
    headers = {}
    api_key = os.environ.get("OPENALEX_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = requests.get(OPENALEX_WORKS_URL, params=params, headers=headers, timeout=20, proxies=get_fetch_proxies())
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.warning(f"OpenAlex DOI lookup failed for {doi!r}: {exc}")
        return FetchResult(doi=doi, source="openalex", error=str(exc))

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

