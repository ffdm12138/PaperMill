"""Conservative publisher PDF lookup from Crossref metadata.

The Crossref work lookup goes through the unified ``ProviderClient``
(shared limiter, retry/backoff, circuit breaker, proxy-configured
transport); only the PDF binary download itself uses ``pdf_transport``.
"""
from loguru import logger

from src.discovery.resolve_crossref import get_crossref_work_by_doi
from src.fetch.models import FetchResult


def resolve_publisher_pdf(doi: str) -> FetchResult:
    message = get_crossref_work_by_doi(doi)
    if message is None:
        logger.warning(f"Publisher PDF lookup failed for {doi!r}")
        return FetchResult(doi=doi, source="publisher", error="crossref work lookup failed")

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
