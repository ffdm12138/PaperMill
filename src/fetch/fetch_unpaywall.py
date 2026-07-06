"""Unpaywall OA PDF lookup."""
import os
from urllib.parse import quote

import requests
from loguru import logger

from src.fetch.models import FetchResult
from src.fetch.proxy import get_fetch_proxies


UNPAYWALL_URL = "https://api.unpaywall.org/v2"


def _location_pdf(location: dict) -> tuple[str, bool]:
    """返回 (url, is_landing_fallback)。

    优先 ``url_for_pdf``（明确 PDF）；仅当无 PDF 字段时 fallback 到 ``url``
    （可能是 landing page），并标记 ``is_landing_fallback=True``。
    """
    url = location.get("url_for_pdf")
    if url:
        return url, False
    url = location.get("url")
    if url:
        return url, True
    return "", False


def resolve_unpaywall(doi: str) -> FetchResult:
    email = os.environ.get("UNPAYWALL_EMAIL", "").strip()
    params = {"email": email or "anonymous@example.com"}
    try:
        response = requests.get(f"{UNPAYWALL_URL}/{quote(doi, safe='')}", params=params, timeout=20, proxies=get_fetch_proxies())
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.warning(f"Unpaywall lookup failed for {doi!r}: {exc}")
        return FetchResult(doi=doi, source="unpaywall", error=str(exc))

    if not data.get("is_oa"):
        return FetchResult(doi=doi, source="unpaywall", oa_status="closed", metadata=data, error="not OA")

    locations = [data.get("best_oa_location") or {}] + list(data.get("oa_locations") or [])
    for location in locations:
        pdf_url, is_landing_fallback = _location_pdf(location)
        if pdf_url:
            meta = dict(data)
            if is_landing_fallback:
                meta["maybe_landing_page"] = True
            candidate_url = f"{UNPAYWALL_URL}/{quote(doi, safe='')}"
            return FetchResult(
                doi=doi,
                success=True,
                source="unpaywall",
                candidate_url=candidate_url,
                pdf_url=pdf_url,
                oa_status=data.get("oa_status") or "oa",
                license=location.get("license") or data.get("license") or "",
                metadata=meta,
            )
    return FetchResult(doi=doi, source="unpaywall", oa_status=data.get("oa_status") or "oa", metadata=data, error="no OA PDF URL")

