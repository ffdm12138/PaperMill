"""Original-link PDF resolver — try URLs already present in metadata first.

Priority within this resolver:
  1. ``metadata.links.pdf_url`` — treated as a direct PDF candidate.
  2. ``metadata.links.url`` / ``publisher_url`` / ``repository_url`` — treated
     as landing pages; HTML is parsed for PDF links.
  3. ``context.source_record`` — CrossRef / OpenAlex / Unpaywall raw records
     with PDF/link fields, loaded at runtime from ``source_records/`` files
     and passed through ``ResolveContext.source_record``.

Inline ``metadata.source.raw_record`` and ``metadata._source_record`` are
forbidden — source records are always loaded by the caller and passed via
``ResolveContext.source_record``.
"""
from __future__ import annotations

from typing import Any

from src.fetch.models import FetchResult
from src.fetch.pdf_transport import fetch_url_direct_then_proxy
from src.fetch.resolvers.base import PdfResolver, ResolveContext
from src.fetch.resolvers.landing_page import (
    extract_landing_candidates,
    resolve_landing_page_to_pdf,
)
from src.fetch.resolvers.url_safety import (
    is_pdf_response,
    is_unsafe_url,
    limit_content,
    looks_like_pdf_url,
    validate_pdf_bytes,
)


FIXED_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)


def _extract_urls_from_source_record(record: dict) -> list[str]:
    """Extract PDF/link candidate URLs from a CrossRef/OpenAlex/Unpaywall
    raw source record.  Returns a deduplicated list of URLs that look
    promising (PDF or landing page).
    """
    urls: list[str] = []

    # Flatten helper
    def _add(val: Any) -> None:
        v = str(val or "").strip()
        if v and v not in urls and v.startswith("http"):
            urls.append(v)

    # Top-level fields common across providers
    for field in ("pdf_url", "url_for_pdf", "pdf", "url",
                  "landing_page_url", "repository_url", "publisher_url",
                  "oa_url"):
        _add(record.get(field))

    # Crossref: record.link[] -> URL, content-type
    links = record.get("link") or []
    if isinstance(links, list):
        for link in links:
            if isinstance(link, dict):
                _add(link.get("URL"))

    # OpenAlex: primary_location, best_oa_location
    for loc_key in ("primary_location", "best_oa_location"):
        loc = record.get(loc_key) or {}
        if isinstance(loc, dict):
            _add(loc.get("pdf_url"))
            _add(loc.get("landing_page_url"))

    # OpenAlex: open_access.oa_url
    oa = record.get("open_access") or {}
    if isinstance(oa, dict):
        _add(oa.get("oa_url"))

    # Unpaywall: best_oa_location, oa_locations
    for loc_key in ("best_oa_location",):
        loc = record.get(loc_key) or {}
        if isinstance(loc, dict):
            _add(loc.get("url_for_pdf"))
            _add(loc.get("url"))

    oa_locs = record.get("oa_locations") or []
    if isinstance(oa_locs, list):
        for loc in oa_locs:
            if isinstance(loc, dict):
                _add(loc.get("url_for_pdf"))
                _add(loc.get("url"))

    # Nested record.record (Crossref style) — only recurse if non-empty
    inner = record.get("record")
    if isinstance(inner, dict) and inner:
        urls.extend(_extract_urls_from_source_record(inner))

    return urls


class OriginalLinkResolver(PdfResolver):
    name = "original_link"
    access_modes = ("oa_only", "institutional", "custom")

    def resolve(self, context: ResolveContext) -> FetchResult:
        meta = context.metadata or {}
        links = meta.get("links") or {}

        # Collect direct PDF candidates (highest priority).
        direct_candidates: list[str] = []
        pdf_url = (links.get("pdf_url") or "").strip()
        if pdf_url:
            direct_candidates.append(pdf_url)

        # Collect landing-page candidates.
        landing_candidates: list[str] = []
        for key in ("url", "publisher_url", "repository_url"):
            val = (links.get(key) or "").strip()
            if val and val not in direct_candidates:
                # If it looks like a PDF URL, treat it as direct instead.
                if looks_like_pdf_url(val):
                    direct_candidates.append(val)
                else:
                    landing_candidates.append(val)

        # Check context.source_record (loaded at runtime by caller from
        # source_records/ files).  Inline raw_record / _source_record are
        # forbidden — source data always comes via ResolveContext.
        source_record = context.source_record or {}
        if isinstance(source_record, dict):
            # Unwrap {"provider": "...", "record": {...}} if present
            inner = source_record.get("record")
            if isinstance(inner, dict):
                for url in _extract_urls_from_source_record(inner):
                    if url in direct_candidates or url in landing_candidates:
                        continue
                    if looks_like_pdf_url(url):
                        direct_candidates.append(url)
                    else:
                        landing_candidates.append(url)
            for url in _extract_urls_from_source_record(source_record):
                if url in direct_candidates or url in landing_candidates:
                    continue
                if looks_like_pdf_url(url):
                    direct_candidates.append(url)
                else:
                    landing_candidates.append(url)

        if not direct_candidates and not landing_candidates:
            return FetchResult(
                doi=context.doi,
                source=self.name,
                resolver=self.name,
                error="no usable original PDF link in metadata",
            )

        # Try direct PDF candidates first.
        last_error = ""
        transport_attempts: list[dict[str, Any]] = []
        for url in direct_candidates:
            result = self._try_direct_pdf(context.doi, url)
            if result.transport_attempts:
                transport_attempts.extend(result.transport_attempts)
            if result.success:
                result.transport_attempts = list(transport_attempts)
                return result
            if result.error:
                last_error = result.error

        # Try landing pages.
        for url in landing_candidates:
            result = self._try_landing_page(context.doi, url)
            if result.transport_attempts:
                transport_attempts.extend(result.transport_attempts)
            if result.success:
                result.transport_attempts = list(transport_attempts)
                return result
            if result.error:
                last_error = result.error

        return FetchResult(
            doi=context.doi,
            source=self.name,
            resolver=self.name,
            error=last_error or "original links did not yield a valid PDF",
            transport_attempts=list(transport_attempts),
        )

    # -- internal helpers ------------------------------------------------

    def _try_direct_pdf(self, doi: str, url: str) -> FetchResult:
        if is_unsafe_url(url):
            return FetchResult(doi=doi, source=self.name, resolver=self.name,
                               candidate_url=url, error="unsafe source blocked")
        transport_attempts: list[dict[str, Any]] = []
        with fetch_url_direct_then_proxy(
            url,
            expected_content="pdf",
            headers={"User-Agent": FIXED_USER_AGENT},
            timeout=30,
            allow_redirects=True,
            stream=True,
        ) as transport:
            transport_attempts.extend(transport.safe_attempts)
            response = transport.response
            if response is None:
                return FetchResult(doi=doi, source=self.name, resolver=self.name,
                                   candidate_url=url, error=transport.error or "transport failed",
                                   transport_attempts=transport_attempts)
            if response.status_code >= 400:
                return FetchResult(doi=doi, source=self.name, resolver=self.name,
                                   candidate_url=url, final_url=response.url or url,
                                   status_code=response.status_code,
                                   content_type=response.headers.get("content-type", ""),
                                   error=f"HTTP {response.status_code}",
                                   transport_attempts=transport_attempts)
            final_url = response.url or url
            status_code = response.status_code if response.status_code else None
            content_type = response.headers.get("content-type", "")
            if is_unsafe_url(final_url):
                return FetchResult(doi=doi, source=self.name, resolver=self.name,
                                   candidate_url=url, final_url=final_url,
                                   status_code=status_code, content_type=content_type,
                                   error=f"unsafe final URL blocked: {final_url}",
                                   transport_attempts=transport_attempts)
            if not is_pdf_response(response):
                return FetchResult(doi=doi, source=self.name, resolver=self.name,
                                   candidate_url=url, final_url=final_url,
                                   status_code=status_code,
                                   content_type=content_type,
                                   error=f"direct link is not a PDF: {content_type}",
                                   transport_attempts=transport_attempts)
            try:
                content = limit_content(response)
            except ValueError as exc:
                return FetchResult(doi=doi, source=self.name, resolver=self.name,
                                   candidate_url=url, final_url=final_url,
                                   status_code=status_code, content_type=content_type,
                                   error=str(exc), transport_attempts=transport_attempts)
            error = validate_pdf_bytes(content)
            if error:
                return FetchResult(doi=doi, source=self.name, resolver=self.name,
                                   candidate_url=url, final_url=final_url,
                                   status_code=status_code,
                                   content_type=content_type,
                                   error=error, transport_attempts=transport_attempts)
            return self._success(doi, content, final_url, final_url, direct=True,
                                 candidate_url=url, status_code=status_code,
                                 content_type=content_type,
                                 transport_attempts=transport_attempts)

        # redirect 后必须检查最终 URL：allow_redirects=True 可能跳到 unsafe host
        final_url = response.url or url
        if is_unsafe_url(final_url):
            return FetchResult(doi=doi, source=self.name, resolver=self.name,
                               candidate_url=url, final_url=final_url,
                               error=f"unsafe final URL blocked: {final_url}")

        if not is_pdf_response(response):
            return FetchResult(doi=doi, source=self.name, resolver=self.name,
                               candidate_url=url, final_url=final_url,
                               status_code=response.status_code if response.status_code else None,
                               content_type=response.headers.get("content-type", ""),
                               error=f"direct link is not a PDF: {response.headers.get('content-type', '')}")

        try:
            content = limit_content(response)
        except ValueError as exc:
            return FetchResult(doi=doi, source=self.name, resolver=self.name,
                               candidate_url=url, final_url=final_url,
                               error=str(exc))

        error = validate_pdf_bytes(content)
        if error:
            return FetchResult(doi=doi, source=self.name, resolver=self.name,
                               candidate_url=url, final_url=final_url,
                               status_code=response.status_code,
                               content_type=response.headers.get("content-type", ""),
                               error=error)

        return self._success(doi, content, final_url, final_url, direct=True,
                               candidate_url=url, status_code=response.status_code,
                               content_type=response.headers.get("content-type", ""))

    def _try_landing_page(self, doi: str, url: str) -> FetchResult:
        if is_unsafe_url(url):
            return FetchResult(doi=doi, source=self.name, resolver=self.name,
                               candidate_url=url, error="unsafe source blocked")
        transport_attempts: list[dict[str, Any]] = []
        with fetch_url_direct_then_proxy(
            url,
            expected_content="html",
            headers={"User-Agent": FIXED_USER_AGENT},
            timeout=30,
            allow_redirects=True,
            stream=True,
        ) as transport:
            transport_attempts.extend(transport.safe_attempts)
            response = transport.response
            if response is None:
                return FetchResult(doi=doi, source=self.name, resolver=self.name,
                                   candidate_url=url, error=transport.error or "transport failed",
                                   transport_attempts=transport_attempts)
            if response.status_code >= 400:
                return FetchResult(doi=doi, source=self.name, resolver=self.name,
                                   candidate_url=url, final_url=response.url or url,
                                   status_code=response.status_code,
                                   content_type=response.headers.get("content-type", ""),
                                   error=f"HTTP {response.status_code}",
                                   transport_attempts=transport_attempts)
            landing_url = response.url or url
            status_code = response.status_code if response.status_code else None
            content_type = response.headers.get("content-type", "")
            if is_unsafe_url(landing_url):
                return FetchResult(doi=doi, source=self.name, resolver=self.name,
                                   error=f"unsafe final URL blocked: {landing_url}",
                                   transport_attempts=transport_attempts)
            if is_pdf_response(response):
                try:
                    content = limit_content(response)
                except ValueError as exc:
                    return FetchResult(doi=doi, source=self.name, resolver=self.name,
                                       error=str(exc), transport_attempts=transport_attempts)
                error = validate_pdf_bytes(content)
                if error:
                    return FetchResult(doi=doi, source=self.name, resolver=self.name,
                                       error=error, transport_attempts=transport_attempts)
                return self._success(doi, content, landing_url, landing_url, direct=True,
                                     candidate_url=url, status_code=status_code,
                                     content_type=content_type,
                                     transport_attempts=transport_attempts)
            content, final_pdf_url, error = resolve_landing_page_to_pdf(
                landing_url,
                timeout_seconds=30,
                headers={"User-Agent": FIXED_USER_AGENT},
                max_depth=3,
                include_known_viewers=True,
                transport_attempts=transport_attempts,
            )
            if content is None:
                return FetchResult(doi=doi, source=self.name, resolver=self.name,
                                   landing_url=landing_url,
                                   error=error or "landing page did not yield a PDF",
                                   transport_attempts=transport_attempts)
            return self._success(doi, content, final_pdf_url, landing_url, direct=False,
                                 candidate_url=url, transport_attempts=transport_attempts)

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
        transport_attempts: list[dict[str, Any]] | None = None,
    ) -> FetchResult:
        return FetchResult(
            doi=doi,
            success=True,
            source=self.name,
            resolver=self.name,
            access_mode="oa_only",
            access_status="original_link",
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
