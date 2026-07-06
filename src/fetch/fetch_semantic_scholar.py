"""Semantic Scholar DOI lookup for OA PDF locations."""
import os
from urllib.parse import quote

import requests
from loguru import logger

from src.fetch.models import FetchResult
from src.fetch.proxy import get_fetch_proxies


S2_PAPER_URL = "https://api.semanticscholar.org/graph/v1/paper"
S2_FIELDS = "title,year,externalIds,url,openAccessPdf,isOpenAccess"


def _headers() -> dict[str, str]:
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    return {"x-api-key": api_key} if api_key else {}


def resolve_semantic_scholar_pdf(doi: str) -> FetchResult:
    try:
        response = requests.get(
            f"{S2_PAPER_URL}/DOI:{quote(doi, safe='')}",
            params={"fields": S2_FIELDS},
            headers=_headers(),
            timeout=20,
            proxies=get_fetch_proxies(),
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.warning(f"Semantic Scholar DOI lookup failed for {doi!r}: {exc}")
        return FetchResult(doi=doi, source="semantic_scholar", error=str(exc))

    pdf = data.get("openAccessPdf") or {}
    if not (data.get("isOpenAccess") and pdf.get("url")):
        return FetchResult(doi=doi, source="semantic_scholar", metadata=data, error="no OA PDF URL")
    return FetchResult(
        doi=doi,
        success=True,
        source="semantic_scholar",
        pdf_url=pdf["url"],
        oa_status="oa",
        metadata=data,
    )

