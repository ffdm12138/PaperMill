"""Conservative publisher PDF lookup from Crossref metadata."""
from urllib.parse import quote

import requests
from loguru import logger

from src.fetch.models import FetchResult
from src.fetch.proxy import get_fetch_proxies


CROSSREF_WORK_URL = "https://api.crossref.org/works"


def resolve_publisher_pdf(doi: str) -> FetchResult:
    try:
        response = requests.get(f"{CROSSREF_WORK_URL}/{quote(doi, safe='')}", timeout=20, proxies=get_fetch_proxies())
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.warning(f"Publisher PDF lookup failed for {doi!r}: {exc}")
        return FetchResult(doi=doi, source="publisher", error=str(exc))

    message = data.get("message") or {}
    licenses = message.get("license") or []
    if not licenses:
        return FetchResult(doi=doi, source="publisher", metadata=message, error="no OA license signal")

    for link in message.get("link") or []:
        content_type = (link.get("content-type") or "").lower()
        url = link.get("URL") or link.get("url") or ""
        if url and ("pdf" in content_type or url.lower().split("?")[0].endswith(".pdf")):
            return FetchResult(
                doi=doi,
                success=True,
                source="publisher",
                pdf_url=url,
                oa_status="oa",
                license=licenses[0].get("URL") or licenses[0].get("content-version") or "",
                metadata=message,
            )
    return FetchResult(doi=doi, source="publisher", metadata=message, error="no PDF link")

