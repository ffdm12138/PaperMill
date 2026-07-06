"""Header-based DOI PDF resolver.

This resolver is explicit custom behavior: callers provide a DOI URL endpoint
and per-run authorization headers, while the User-Agent remains fixed here.
"""
from __future__ import annotations

from typing import Any

import requests

from src.fetch.models import FetchResult
from src.fetch.proxy import get_fetch_proxies
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


class HeaderBasedDoiResolver(PdfResolver):
    name = "header_based"
    access_modes = ("custom", "institutional")

    def __init__(
        self,
        *,
        base_url: str = "",
        url_template: str = "",
        headers: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> None:
        self.base_url = base_url
        self.url_template = url_template
        self.headers = dict(headers or {})
        self.timeout = timeout

    def resolve(self, context: ResolveContext) -> FetchResult:
        try:
            candidate_url = self._url_for(context.doi)
        except ValueError as exc:
            return FetchResult(doi=context.doi, source=self.name, resolver=self.name, error=str(exc))

        if is_unsafe_url(candidate_url):
            return FetchResult(
                doi=context.doi,
                source=self.name,
                resolver=self.name,
                candidate_url=candidate_url,
                error="unsafe source blocked",
            )

        request_headers = self._headers()
        try:
            response = requests.get(
                candidate_url,
                headers=request_headers,
                timeout=self.timeout,
                allow_redirects=True,
                proxies=get_fetch_proxies(),
                stream=True,
            )
            response.raise_for_status()
            status_code = response.status_code
            content_type = response.headers.get("content-type", "")
        except Exception as exc:
            return FetchResult(doi=context.doi, source=self.name, resolver=self.name,
                               candidate_url=candidate_url, error=str(exc))

        # redirect 后必须检查最终 URL：allow_redirects=True 可能跳到 unsafe host
        landing_url = response.url or candidate_url
        if is_unsafe_url(landing_url):
            return FetchResult(
                doi=context.doi,
                source=self.name,
                resolver=self.name,
                candidate_url=candidate_url, final_url=landing_url,
                status_code=status_code, content_type=content_type,
                error=f"unsafe final URL blocked: {landing_url}",
            )
        if is_pdf_response(response):
            content = limit_content(response)
            error = validate_pdf_bytes(content)
            if error:
                return FetchResult(doi=context.doi, source=self.name, resolver=self.name,
                                   candidate_url=candidate_url, final_url=landing_url,
                                   status_code=status_code, content_type=content_type, error=error)
            return self._success(context.doi, content, landing_url, landing_url, direct=True,
                                 candidate_url=candidate_url, status_code=status_code,
                                 content_type=content_type)

        # HTML response — use multi-level landing page resolution to find
        # the real PDF.  This handles viewer portals (SciCloud etc.) that
        # don't expose direct .pdf links.
        content, final_pdf_url, error = resolve_landing_page_to_pdf(
            landing_url,
            timeout_seconds=self.timeout,
            headers=request_headers,
            max_depth=3,
            include_known_viewers=True,
        )
        if content is None:
            return FetchResult(
                doi=context.doi,
                source=self.name,
                resolver=self.name,
                candidate_url=candidate_url, final_url=landing_url,
                status_code=status_code, content_type=content_type,
                landing_url=landing_url,
                error=error or "HTML response did not contain a PDF link",
            )
        return self._success(context.doi, content, final_pdf_url, landing_url, direct=False,
                             candidate_url=candidate_url, status_code=status_code,
                             content_type=content_type)

    def _url_for(self, doi: str) -> str:
        from urllib.parse import quote
        if self.url_template:
            return self.url_template.format(
                doi=quote(doi, safe=""),
                doi_raw=doi,
                doi_path=quote(doi, safe="/"),
                doi_query=quote(doi, safe=""),
            )
        if self.base_url:
            # When base_url is used as a path prefix (not a query), the '/'
            # in the DOI should be preserved.  Use {doi_query} in url_template
            # for the query-string (encoded) form.
            return f"{self.base_url}{quote(doi, safe='/')}"
        # Default: resolve from the canonical DOI landing page.
        # In the path the '/' MUST NOT be encoded as %2F.
        return f"https://doi.org/{quote(doi, safe='/')}"

    def _headers(self) -> dict[str, str]:
        headers = {k: v for k, v in self.headers.items() if k.lower() != "user-agent"}
        headers["User-Agent"] = FIXED_USER_AGENT
        return headers

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
    ) -> FetchResult:
        metadata: dict[str, Any] = {
            "fixed_user_agent": True,
            "header_keys": sorted(self._headers().keys()),
            "headers_masked": True,
            "requires_user_headers": bool(self.headers),
        }
        return FetchResult(
            doi=doi,
            success=True,
            source=self.name,
            resolver=self.name,
            access_mode="custom",
            access_status="authorized_header",
            pdf_url=pdf_url,
            landing_url=landing_url,
            is_direct_pdf=direct,
            candidate_url=candidate_url,
            final_url=pdf_url,
            status_code=status_code,
            content_type=content_type,
            raw={"content": content, **metadata},
            metadata=metadata,
        )
