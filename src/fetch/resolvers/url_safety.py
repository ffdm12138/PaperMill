"""Shared URL safety and PDF validation helpers for fetch resolvers.

Extracted from ``header_based_resolver.py`` so that ``original_link_resolver``
and other resolvers can reuse the same unsafe-host blocking, PDF magic-byte
validation, and HTML PDF-link extraction logic.
"""
from __future__ import annotations

from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from config.settings import MINERU_FETCH_MAX_BYTES

#: Host fragments that are permanently blocked — no CLI flag can override.
UNSAFE_HOST_FRAGMENTS = (
    "sci-hub",
    "libgen",
    "z-lib",
    "zlibrary",
    "annas-archive",
)


def looks_like_pdf_url(url: str) -> bool:
    """Heuristic: does *url* look like a PDF link?"""
    lower = (url or "").lower()
    return ".pdf" in lower.split("#", 1)[0].split("?", 1)[0] or "pdf" in lower


def is_pdf_response(response: requests.Response) -> bool:
    """Check content-type header or final URL for PDF indicators."""
    content_type = response.headers.get("content-type", "").lower()
    url = response.url or ""
    return "pdf" in content_type or looks_like_pdf_url(url)


def is_unsafe_url(url: str) -> bool:
    """Return True if *url* host matches any unsafe host fragment."""
    host = (urlparse(url).hostname or "").lower()
    return any(fragment in host for fragment in UNSAFE_HOST_FRAGMENTS)


def limit_content(response: requests.Response) -> bytes:
    """Return response content as bytes, enforcing MINERU_FETCH_MAX_BYTES.

    Bounded streaming: never reads the full body into memory before checking
    the size. First inspects ``Content-Length`` and fails early if it exceeds
    the limit; then streams via ``iter_content`` and aborts as soon as the
    running total crosses the limit. This avoids loading a large PDF or a
    huge error page entirely into memory before rejecting it.
    """
    # 1. Pre-check Content-Length header (fast path, no body read).
    try:
        declared = int(response.headers.get("Content-Length", "") or "")
    except (TypeError, ValueError):
        declared = -1
    if declared > MINERU_FETCH_MAX_BYTES:
        raise ValueError(f"PDF exceeds MINERU_FETCH_MAX_BYTES={MINERU_FETCH_MAX_BYTES}")

    # 2. Stream the body in bounded chunks, aborting once the limit is crossed.
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > MINERU_FETCH_MAX_BYTES:
            raise ValueError(f"PDF exceeds MINERU_FETCH_MAX_BYTES={MINERU_FETCH_MAX_BYTES}")
        chunks.append(chunk)
    return b"".join(chunks)


def validate_pdf_bytes(content: bytes) -> str:
    """Return empty string if *content* is a valid PDF, else an error message."""
    if not content:
        return "empty PDF response"
    if not content.startswith(b"%PDF"):
        return "response content is not a valid PDF"
    return ""


class PdfLinkParser(HTMLParser):
    """Extract candidate PDF links from ``<a>``/``<iframe>``/``<embed>`` tags."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() not in {"a", "iframe", "embed"}:
            return
        attrs_dict = {k.lower(): v for k, v in attrs if v}
        href = attrs_dict.get("href") or attrs_dict.get("src") or ""
        if looks_like_pdf_url(href):
            self.links.append(href)


def extract_pdf_url_from_html(html_text: str, base_url: str) -> str:
    """Parse *html_text* and return the first PDF link resolved against *base_url*."""
    parser = PdfLinkParser()
    try:
        parser.feed(html_text)
    except Exception:
        return ""
    if not parser.links:
        return ""
    return urljoin(base_url, parser.links[0])
