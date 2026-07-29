"""Publisher TDM and direct-PDF resolvers."""
from __future__ import annotations

from typing import Any

from loguru import logger

from config.settings import ELSEVIER_API_KEY, WILEY_TDM_TOKEN
from src.fetch.models import FetchResult
from src.fetch.pdf_transport import fetch_url_direct_then_proxy
from src.fetch.resolvers.url_safety import is_unsafe_url, limit_content, validate_pdf_bytes

from .base import PdfResolver, ResolveContext

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _pdf_response_ok(response) -> bool:
    return response.status_code == 200 and "application/pdf" in response.headers.get("Content-Type", "").lower()


def _content_result(
    *,
    doi: str,
    resolver: str,
    response,
    url: str,
    transport_attempts: list[dict[str, Any]],
    pdf_url: str = "",
) -> FetchResult:
    final_url = response.url or url
    if response.status_code >= 400:
        return FetchResult(
            doi=doi,
            resolver=resolver,
            source=resolver,
            status_code=response.status_code,
            final_url=final_url,
            content_type=response.headers.get("Content-Type", ""),
            error=f"HTTP {response.status_code}",
            transport_attempts=list(transport_attempts),
        )
    if is_unsafe_url(final_url):
        return FetchResult(
            doi=doi,
            resolver=resolver,
            source=resolver,
            final_url=final_url,
            error=f"unsafe final URL blocked: {final_url}",
            transport_attempts=list(transport_attempts),
        )
    if not _pdf_response_ok(response):
        return FetchResult(
            doi=doi,
            resolver=resolver,
            source=resolver,
            status_code=response.status_code,
            final_url=final_url,
            content_type=response.headers.get("Content-Type", ""),
            error=f"{resolver} failed: HTTP {response.status_code}",
            transport_attempts=list(transport_attempts),
        )
    content = limit_content(response)
    error = validate_pdf_bytes(content)
    if error:
        return FetchResult(
            doi=doi,
            resolver=resolver,
            source=resolver,
            status_code=response.status_code,
            final_url=final_url,
            content_type=response.headers.get("Content-Type", ""),
            error=error,
            transport_attempts=list(transport_attempts),
        )
    logger.info("[tdm] {} OK: {} ({}KB)", resolver, doi, len(content) // 1024)
    return FetchResult(
        doi=doi,
        success=True,
        source=resolver,
        resolver=resolver,
        pdf_url=pdf_url,
        output_path="",
        raw={"content": content, "status_code": response.status_code},
        access_status="open_access",
        status_code=response.status_code,
        final_url=final_url,
        content_type=response.headers.get("Content-Type", ""),
        transport_attempts=list(transport_attempts),
    )


class WileyTdmResolver(PdfResolver):
    """Wiley TDM API PDF resolver."""

    name = "wiley_tdm"
    access_modes = ("institutional", "custom")

    #: Wiley-issued DOI prefixes (Wiley proper, Wiley-Blackwell, AGU).
    DOI_PREFIXES = ("10.1002/", "10.1111/", "10.1029/")

    def applies_to(self, context: ResolveContext) -> bool:
        return str(context.doi or "").startswith(self.DOI_PREFIXES)

    def resolve(self, context: ResolveContext) -> FetchResult:
        doi = context.doi
        if not doi.startswith(self.DOI_PREFIXES):
            return FetchResult(doi=doi, source=self.name, resolver=self.name, error="not a Wiley DOI prefix")

        if not WILEY_TDM_TOKEN:
            return FetchResult(doi=doi, source=self.name, resolver=self.name, error="WILEY_TDM_TOKEN not configured; skip")

        token = WILEY_TDM_TOKEN
        url = f"https://api.wiley.com/onlinelibrary/tdm/v1/articles/{doi}"
        headers = {
            "User-Agent": USER_AGENT,
            "Wiley-TDM-Client-Token": token,
        }
        transport_attempts: list[dict[str, Any]] = []
        with fetch_url_direct_then_proxy(
            url,
            expected_content="pdf",
            headers=headers,
            allow_redirects=True,
            stream=True,
        ) as transport:
            transport_attempts.extend(transport.safe_attempts)
            if transport.response is None:
                return FetchResult(
                    doi=doi,
                    source=self.name,
                    resolver=self.name,
                    error=f"Wiley TDM error: {transport.error or 'transport failed'}",
                    transport_attempts=transport_attempts,
                )
            return _content_result(
                doi=doi,
                resolver=self.name,
                response=transport.response,
                url=url,
                transport_attempts=transport_attempts,
            )


class SpringerDirectResolver(PdfResolver):
    """Springer Nature direct PDF URL resolver."""

    name = "springer_direct"
    access_modes = ("oa_only", "institutional")

    #: Springer Nature DOI prefixes (Springer, BMC, Nature, IBM Journals).
    DOI_PREFIXES = ("10.1007/", "10.1186/", "10.1038/", "10.1147/")

    def applies_to(self, context: ResolveContext) -> bool:
        return str(context.doi or "").startswith(self.DOI_PREFIXES)

    def resolve(self, context: ResolveContext) -> FetchResult:
        doi = context.doi
        if not doi.startswith(self.DOI_PREFIXES):
            return FetchResult(doi=doi, source=self.name, resolver=self.name, error="not a Springer/Nature DOI prefix")

        url = f"https://link.springer.com/content/pdf/{doi}.pdf"
        headers = {"User-Agent": USER_AGENT}
        transport_attempts: list[dict[str, Any]] = []
        with fetch_url_direct_then_proxy(
            url,
            expected_content="pdf",
            headers=headers,
            allow_redirects=True,
            stream=True,
        ) as transport:
            transport_attempts.extend(transport.safe_attempts)
            if transport.response is None:
                return FetchResult(
                    doi=doi,
                    source=self.name,
                    resolver=self.name,
                    error=f"Springer direct error: {transport.error or 'transport failed'}",
                    transport_attempts=transport_attempts,
                )
            return _content_result(
                doi=doi,
                resolver=self.name,
                response=transport.response,
                url=url,
                transport_attempts=transport_attempts,
            )


class ElsevierTdmResolver(PdfResolver):
    """Elsevier article retrieval PDF resolver."""

    name = "elsevier_tdm"
    access_modes = ("oa_only", "institutional")

    #: Elsevier-issued DOI prefixes.
    DOI_PREFIXES = ("10.1016/", "10.1011/")

    def applies_to(self, context: ResolveContext) -> bool:
        return str(context.doi or "").startswith(self.DOI_PREFIXES)

    def resolve(self, context: ResolveContext) -> FetchResult:
        doi = context.doi
        if not doi.startswith(self.DOI_PREFIXES):
            return FetchResult(doi=doi, source=self.name, resolver=self.name, error="not an Elsevier DOI prefix")
        if not ELSEVIER_API_KEY:
            return FetchResult(doi=doi, source=self.name, resolver=self.name, error="ELSEVIER_API_KEY not configured; skip")

        url = f"https://api.elsevier.com/content/article/doi/{doi}"
        headers = {
            "User-Agent": USER_AGENT,
            "X-ELS-APIKey": ELSEVIER_API_KEY,
            "Accept": "application/pdf",
        }
        transport_attempts: list[dict[str, Any]] = []
        with fetch_url_direct_then_proxy(
            url,
            expected_content="pdf",
            headers=headers,
            allow_redirects=True,
            stream=True,
        ) as transport:
            transport_attempts.extend(transport.safe_attempts)
            if transport.response is None:
                return FetchResult(
                    doi=doi,
                    source=self.name,
                    resolver=self.name,
                    error=f"Elsevier TDM error: {transport.error or 'transport failed'}",
                    transport_attempts=transport_attempts,
                )
            return _content_result(
                doi=doi,
                resolver=self.name,
                response=transport.response,
                url=url,
                pdf_url=transport.response.url or url,
                transport_attempts=transport_attempts,
            )
