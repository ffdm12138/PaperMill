"""Landing page PDF resolution utilities.

Shared helpers for resolving HTML landing pages to actual PDF URLs.
Extracted so that ``OriginalLinkResolver``, ``HeaderBasedDoiResolver``,
``SciEngineResolver``, and the fetch pipeline can reuse the same logic
without duplication.

Key additions over ``url_safety.py``:
- Recognises known PDF viewer / download portal URLs (SciCloud, etc.)
- Multi-level landing page resolution (follow redirects / viewer pages)
- Candidate sorting: direct PDF > download > viewer > weak anchor-text hints
- Anchor text scoring for link prioritisation
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

from loguru import logger

from src.fetch.pdf_transport import fetch_url_direct_then_proxy
from src.fetch.resolvers.url_safety import (
    is_pdf_response,
    is_unsafe_url,
    limit_content,
    looks_like_pdf_url,
    validate_pdf_bytes,
)

# ── Known viewer / download portal URL fragments ──────────────────────
# These are NOT direct PDF URLs, but known landing/viewer pages that may
# embed or redirect to a PDF.  We want to follow them (recursively) to
# find the real PDF.

KNOWN_VIEWER_FRAGMENTS = (
    "/fileNotLogin/view/",
    "/doi/pdf/",
    "/parse/pdf/",
    "/download/",
    "/pdfdirect/",
    "/epdf/",
    "/doi/epdf/",
    "scicloudcenter.com",
)

# Anchor text keywords that weakly suggest a PDF/download link.
# Scored lower than structural URL matches.
ANCHOR_PDF_KEYWORDS = (
    "pdf", "full text", "download", "全文", "下载", "免费获取",
    "free access", "open access", "oa",
)


def looks_like_known_viewer_url(url: str) -> bool:
    """Return True if *url* is a known PDF viewer / download portal page.

    These URLs don't contain ``.pdf`` but are worth following because they
    may embed or redirect to a real PDF.
    """
    lower = (url or "").lower()
    return any(fragment.lower() in lower for fragment in KNOWN_VIEWER_FRAGMENTS)


def _anchor_score(anchor_text: str) -> int:
    """Return a score (higher = more promising) based on anchor text."""
    if not anchor_text:
        return 0
    lower = anchor_text.strip().lower()
    score = 0
    for kw in ANCHOR_PDF_KEYWORDS:
        if kw in lower:
            score += 1
    return score


# ── HTML candidate extraction ─────────────────────────────────────────

class LandingCandidateParser(HTMLParser):
    """Extract candidate links from HTML, with scoring.

    Collects links from:
    - ``<a href="">`` (scored at </a> with anchor text)
    - ``<iframe src="">``
    - ``<embed src="">``
    - ``<object data="">``
    - ``<meta content="">`` (citation_pdf_url and similar)

    Each candidate is a tuple of ``(url, score)`` where higher scores are
    more promising.  <a> tags are deferred to ``handle_endtag`` so that
    anchor text ("免费获取", "PDF", "Download", etc.) is available for scoring.
    """

    def __init__(self, *, include_known_viewers: bool = True) -> None:
        super().__init__()
        self.candidates: list[tuple[str, int]] = []
        self._include_known_viewers = include_known_viewers
        self._current_anchor: str = ""
        # Deferred <a> state: (href, base_url_score)
        self._pending_a: tuple[str, int] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        attrs_dict: dict[str, str] = {}
        for k, v in attrs:
            if v:
                attrs_dict[k.lower()] = v

        if tag_lower == "a":
            # Defer scoring until </a> when anchor text is available.
            href = attrs_dict.get("href") or ""
            if href:
                base_score = self._score_url(href)
                # If the URL itself already has a strong score, we can still
                # add it now; otherwise defer for anchor text.
                if base_score >= 80:
                    self.candidates.append((href, base_score))
                    self._pending_a = None
                elif base_score > 0:
                    self._pending_a = (href, base_score)
                else:
                    self._pending_a = (href, 0)  # may get anchor text boost
            else:
                self._pending_a = None
            self._current_anchor = ""

        elif tag_lower in ("iframe", "embed"):
            href = attrs_dict.get("href") or attrs_dict.get("src") or ""
            if href:
                score = self._score_url(href)
                if score > 0:
                    self.candidates.append((href, score))

        elif tag_lower == "object":
            data = attrs_dict.get("data") or ""
            if data:
                score = self._score_url(data)
                if score > 0:
                    self.candidates.append((data, score))

        elif tag_lower == "meta":
            name = (attrs_dict.get("name") or "").lower()
            content = attrs_dict.get("content") or ""
            if content and name in (
                "citation_pdf_url",
                "citation_fulltext_html_url",
                "dc.identifier",
            ):
                score = self._score_url(content)
                if score > 0:
                    self.candidates.append((content, score))

    def handle_data(self, data: str) -> None:
        self._current_anchor += data

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a":
            if self._pending_a is not None:
                href, base_score = self._pending_a
                # Combine URL score + anchor text score
                anchor_score = _anchor_score(self._current_anchor)
                total = base_score + anchor_score
                if total > 0:
                    self.candidates.append((href, total))
                self._pending_a = None
            self._current_anchor = ""

    def _score_url(self, href: str) -> int:
        """Score a URL: higher = more likely to be a PDF or PDF-viewer page."""
        if not href or href.startswith("#") or href.startswith("javascript:"):
            return 0

        lower = href.lower()

        # Direct PDF URL
        if looks_like_pdf_url(href):
            return 100

        # Known viewer / download portal (e.g. SciCloud fileNotLogin/view)
        if self._include_known_viewers and looks_like_known_viewer_url(href):
            return 80

        # Generic download hint in URL path/query
        for kw in ("/download/", "/pdf/", "download=", "format=pdf", "type=pdf"):
            if kw in lower:
                return 50

        return 0


def extract_landing_candidates(
    html: str,
    base_url: str,
    *,
    include_known_viewers: bool = True,
) -> list[str]:
    """Parse HTML and return sorted candidate URLs (best first).

    Resolves relative URLs against *base_url*.  Duplicates are removed
    while preserving the highest score.
    """
    parser = LandingCandidateParser(include_known_viewers=include_known_viewers)
    try:
        parser.feed(html)
    except Exception:
        return []

    # Deduplicate by resolved URL, keeping highest score
    seen: dict[str, int] = {}
    for raw_url, score in parser.candidates:
        resolved = urljoin(base_url, raw_url)
        if resolved not in seen or score > seen[resolved]:
            seen[resolved] = score

    # Sort by score descending, then by URL for determinism
    sorted_urls = sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))
    return [url for url, _score in sorted_urls]


# ── Script-based URL extraction (conservative regex, no JS execution) ──

_SCRIPT_URL_RE = re.compile(
    r"""(?:"|')((?:https?:)?//[^"'\s]+?\.[^"'\s]{1,8})["']""",
    re.IGNORECASE,
)


def extract_urls_from_scripts(html: str) -> list[str]:
    """Extract URLs from ``<script>`` blocks using conservative regex.

    This is a best-effort heuristic — no JavaScript execution is performed.
    """
    urls: list[str] = []
    for match in _SCRIPT_URL_RE.finditer(html):
        url = match.group(1)
        if url and len(url) > 10:  # skip trivial fragments
            urls.append(url)
    return urls


# ── Multi-level landing page resolution ───────────────────────────────

MAX_LANDING_DEPTH = 3
LANDING_TIMEOUT = 30


def resolve_landing_page_to_pdf(
    url: str,
    *,
    timeout_seconds: int = LANDING_TIMEOUT,
    headers: dict[str, str] | None = None,
    max_depth: int = MAX_LANDING_DEPTH,
    include_known_viewers: bool = True,
    transport_attempts: list[dict[str, Any]] | None = None,
) -> tuple[bytes | None, str, str]:
    """Follow a landing page URL recursively until a PDF is found.

    Returns ``(pdf_bytes, final_pdf_url, landing_url)`` on success, or
    ``(None, "", reason)`` on failure.  *reason* explains why no PDF was
    found (e.g. "no PDF candidates", "max depth exceeded").

    Recursion depth is limited to *max_depth* to prevent infinite crawls.
    URLs are deduplicated via ``visited`` set to avoid cycles.
    """
    req_headers = dict(headers or {})
    visited: set[str] = set()
    current_url = url
    landing_url = url
    depth = 0

    while depth < max_depth:
        if current_url in visited:
            return None, "", "landing page cycle detected"
        visited.add(current_url)

        if is_unsafe_url(current_url):
            return None, "", f"unsafe URL blocked: {current_url}"

        transport_ctx = fetch_url_direct_then_proxy(
            current_url,
            expected_content="html",
            headers=req_headers,
            timeout=timeout_seconds,
            allow_redirects=True,
            stream=True,
        )
        with transport_ctx as transport:
            if transport_attempts is not None:
                transport_attempts.extend(transport.safe_attempts)
            response = transport.response
            if response is None:
                return None, "", f"HTTP error at depth {depth}: {transport.error or 'transport failed'}"
            if response.status_code >= 400:
                return None, "", f"HTTP error at depth {depth}: HTTP {response.status_code}"

            final_url = response.url or current_url
            if is_unsafe_url(final_url):
                return None, "", f"unsafe final URL blocked: {final_url}"

            # Remember the first landing URL
            if depth == 0:
                landing_url = final_url

            # Direct PDF response
            if is_pdf_response(response):
                try:
                    content = limit_content(response)
                except ValueError as exc:
                    return None, "", f"content limit exceeded: {exc}"
                error = validate_pdf_bytes(content)
                if error:
                    return None, "", error
                return content, final_url, landing_url

            html_text = ""
            try:
                html_text = response.text
            except Exception:
                pass

            if html_text:
                candidates = extract_landing_candidates(
                    html_text,
                    final_url,
                    include_known_viewers=include_known_viewers,
                )

                script_urls = extract_urls_from_scripts(html_text)
                for u in script_urls:
                    resolved = urljoin(final_url, u)
                    if resolved not in candidates:
                        if looks_like_pdf_url(u) or looks_like_known_viewer_url(u):
                            candidates.append(resolved)

                best_reason = ""
                for candidate in candidates:
                    if candidate in visited:
                        continue
                    content, pdf_url, reason = resolve_landing_page_to_pdf(
                        candidate,
                        timeout_seconds=timeout_seconds,
                        headers=req_headers,
                        max_depth=max_depth - depth - 1,
                        include_known_viewers=include_known_viewers,
                        transport_attempts=transport_attempts,
                    )
                    if content is not None:
                        return content, pdf_url, landing_url
                    if reason and not best_reason:
                        best_reason = reason
                    if reason and ("unsafe" in reason.lower() or "blocked" in reason.lower()):
                        best_reason = reason

                if best_reason:
                    return None, "", best_reason

            if depth == 0:
                return None, "", f"landing page did not contain PDF candidates (url: {final_url})"
            return None, "", f"no PDF at depth {depth}"

    return None, "", f"max depth {max_depth} exceeded without finding PDF"


# ── Simple convenience wrappers ────────────────────────────────────────

def try_resolve_landing_to_pdf(
    url: str,
    *,
    timeout_seconds: int = LANDING_TIMEOUT,
    headers: dict[str, str] | None = None,
    max_depth: int = MAX_LANDING_DEPTH,
    transport_attempts: list[dict[str, Any]] | None = None,
) -> tuple[bytes | None, str]:
    """Convenience: return (content, error).

    ``content`` is bytes on success, None on failure. ``error`` is empty
    on success.
    """
    content, pdf_url, detail = resolve_landing_page_to_pdf(
        url,
        timeout_seconds=timeout_seconds,
        headers=headers,
        max_depth=max_depth,
        transport_attempts=transport_attempts,
    )
    if content is not None:
        return content, ""
    return None, detail
