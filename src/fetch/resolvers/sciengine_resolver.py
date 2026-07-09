"""SciEngine / SciCloud DOI PDF resolver.

Handles Science China Press (``10.1360/``) DOIs by resolving through
``sciengine.com`` and ``scicloudcenter.com`` viewer portals.

This resolver is publisher-specific: only activates for DOIs matching
``10.1360/``.  It uses the shared ``landing_page.py`` multi-level
resolution to navigate the SciCloud ``fileNotLogin/view`` → PDF chain.
"""
from __future__ import annotations

from urllib.parse import quote

from src.fetch.models import FetchResult
from src.fetch.pdf_transport import fetch_url_direct_then_proxy
from src.fetch.resolvers.base import PdfResolver, ResolveContext
from src.fetch.resolvers.url_safety import (
    is_pdf_response,
    is_unsafe_url,
    limit_content,
    validate_pdf_bytes,
)
from src.fetch.resolvers.landing_page import (
    resolve_landing_page_to_pdf,
)

FIXED_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)

# Only activate for Science China / SciEngine DOIs.
SCIENGINE_DOI_PREFIX = "10.1360/"

# Candidate landing-page URLs to try, in priority order.
# {doi_path} uses safe='/' so the DOI slash stays unencoded.
_SCIENGINE_CANDIDATES = (
    "https://www.sciengine.com/doi/{doi_path}",
    "https://www.sciengine.com/SSTe/doi/{doi_path}",
    "https://doi.org/{doi_path}",
)


class SciEngineResolver(PdfResolver):
    """Resolve SciEngine / Science China Press DOIs to PDFs.

    Only activates when the DOI starts with ``10.1360/`` (case-insensitive
    comparison), so it does not add overhead for non-matching DOIs.
    """

    name = "sciengine_direct"
    access_modes = ("oa_only", "custom", "institutional")

    def __init__(self, *, timeout: int = 30) -> None:
        self.timeout = timeout

    def resolve(self, context: ResolveContext) -> FetchResult:
        doi = context.doi

        # Only activate for SciEngine DOIs.
        if not doi.lower().startswith(SCIENGINE_DOI_PREFIX):
            return FetchResult(
                doi=doi,
                source=self.name,
                resolver=self.name,
                error="not a SciEngine DOI (prefix 10.1360/)",
            )

        doi_path = quote(doi, safe="/")
        headers = {"User-Agent": FIXED_USER_AGENT}

        last_error = ""
        transport_attempts: list[dict[str, object]] = []
        for template in _SCIENGINE_CANDIDATES:
            candidate_url = template.format(doi_path=doi_path)

            if is_unsafe_url(candidate_url):
                last_error = f"unsafe URL blocked: {candidate_url}"
                continue

            # Step 1: fetch the landing page
            with fetch_url_direct_then_proxy(
                candidate_url,
                expected_content="html",
                headers=headers,
                timeout=self.timeout,
                allow_redirects=True,
                stream=True,
            ) as transport:
                transport_attempts.extend(transport.safe_attempts)
                response = transport.response
                if response is None:
                    last_error = transport.error or "transport failed"
                    continue
                if response.status_code >= 400:
                    last_error = f"HTTP {response.status_code}"
                    continue
                landing_url = response.url or candidate_url
                sc = response.status_code
                ct = response.headers.get("content-type", "")
                if is_unsafe_url(landing_url):
                    last_error = f"unsafe final URL: {landing_url}"
                    continue

                if is_pdf_response(response):
                    try:
                        content = limit_content(response)
                    except ValueError as exc:
                        last_error = str(exc)
                        continue
                    error = validate_pdf_bytes(content)
                    if error:
                        last_error = error
                        continue
                    return self._success(doi, content, landing_url, landing_url, direct=True,
                                         candidate_url=candidate_url, status_code=sc, content_type=ct,
                                         transport_attempts=transport_attempts)

                content, final_pdf_url, error = resolve_landing_page_to_pdf(
                    landing_url,
                    timeout_seconds=self.timeout,
                    headers=headers,
                    max_depth=3,
                    include_known_viewers=True,
                    transport_attempts=transport_attempts,
                )
                if content is not None:
                    return self._success(doi, content, final_pdf_url, landing_url, direct=False,
                                         candidate_url=candidate_url, status_code=sc, content_type=ct,
                                         transport_attempts=transport_attempts)

                last_error = error or "landing page did not yield a PDF"
                continue

        return FetchResult(
            doi=doi,
            source=self.name,
            resolver=self.name,
            error=last_error or "no PDF found via SciEngine",
            transport_attempts=list(transport_attempts),
        )

    def _success(
        self,
        doi: str,
        content: bytes,
        pdf_url: str,
        landing_url: str,
        *,
        direct: bool,
        candidate_url: str = "",
        status_code: int | None = None,
        content_type: str = "",
        transport_attempts: list[dict[str, object]] | None = None,
    ) -> FetchResult:
        return FetchResult(
            doi=doi,
            success=True,
            source=self.name,
            resolver=self.name,
            access_mode="oa_only",
            access_status="publisher_oa",
            pdf_url=pdf_url,
            landing_url=landing_url,
            is_direct_pdf=direct,
            candidate_url=candidate_url,
            final_url=pdf_url,
            status_code=status_code,
            content_type=content_type,
            raw={"content": content},
            transport_attempts=list(transport_attempts or []),
        )
