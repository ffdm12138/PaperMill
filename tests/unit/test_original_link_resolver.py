"""Tests for OriginalLinkResolver — tries metadata links first before OA resolvers."""
import pytest

from src.fetch.resolvers.base import ResolveContext
from src.fetch.resolvers.original_link_resolver import OriginalLinkResolver


class FakeResponse:
    def __init__(self, *, url: str, content: bytes, content_type: str, status_code: int = 200):
        self.url = url
        self.content = content
        self.headers = {"content-type": content_type}
        self.status_code = status_code
        self.text = content.decode("utf-8", errors="ignore")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1024):
        """Yield content in chunks to mimic streaming responses."""
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]


def _ctx(metadata: dict) -> ResolveContext:
    return ResolveContext(doi="10.1000/test", metadata=metadata)


# ── 1. metadata.links.pdf_url is a direct PDF → success ──────────────

def test_direct_pdf_url_success(monkeypatch, install_pdf_transport_get):
    def fake_get(url, **kwargs):
        return FakeResponse(url=url, content=b"%PDF direct content", content_type="application/pdf")

    install_pdf_transport_get(fake_get)
    resolver = OriginalLinkResolver()
    ctx = _ctx({"links": {"pdf_url": "https://example.test/paper.pdf"}})

    result = resolver.resolve(ctx)

    assert result.success is True
    assert result.is_direct_pdf is True
    assert result.raw["content"] == b"%PDF direct content"
    assert result.pdf_url == "https://example.test/paper.pdf"


# ── 2. landing page HTML contains PDF link → success ─────────────────

def test_landing_page_pdf_link_success(monkeypatch, install_pdf_transport_get):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "paper.pdf" in url:
            return FakeResponse(url=url, content=b"%PDF linked", content_type="application/pdf")
        return FakeResponse(
            url="https://example.test/landing",
            content=b'<html><a href="/paper.pdf">PDF</a></html>',
            content_type="text/html",
        )

    install_pdf_transport_get(fake_get)
    resolver = OriginalLinkResolver()
    ctx = _ctx({"links": {"url": "https://example.test/landing"}})

    result = resolver.resolve(ctx)

    assert result.success is True
    assert result.is_direct_pdf is False
    assert result.landing_url == "https://example.test/landing"


# ── 3. content-type is PDF but bytes don't start with %PDF → fail ─────

def test_content_type_pdf_but_invalid_magic_rejected(monkeypatch, install_pdf_transport_get):
    def fake_get(url, **kwargs):
        return FakeResponse(url=url, content=b"not a real pdf", content_type="application/pdf")

    install_pdf_transport_get(fake_get)
    resolver = OriginalLinkResolver()
    ctx = _ctx({"links": {"pdf_url": "https://example.test/fake.pdf"}})

    result = resolver.resolve(ctx)

    assert result.success is False
    assert "valid PDF" in result.error


# ── 4. unsafe host is blocked, no network request ────────────────────

def test_unsafe_host_blocked_without_network(monkeypatch, install_pdf_transport_get):
    def fail_get(*args, **kwargs):
        raise AssertionError("unsafe host should be blocked before network")

    install_pdf_transport_get(fail_get)
    resolver = OriginalLinkResolver()
    ctx = _ctx({"links": {"pdf_url": "https://sci-hub.se/10.1000/test"}})

    result = resolver.resolve(ctx)

    assert result.success is False
    assert "unsafe" in result.error


# ── 5. no original links → fail without exception ────────────────────

def test_no_links_returns_failure(monkeypatch, install_pdf_transport_get):
    def fail_get(*args, **kwargs):
        raise AssertionError("no network call expected when there are no links")

    install_pdf_transport_get(fail_get)
    resolver = OriginalLinkResolver()
    ctx = _ctx({"links": {}})

    result = resolver.resolve(ctx)

    assert result.success is False
    assert "no usable original" in result.error


# ── 6. unsafe PDF link extracted from landing page is blocked ─────────

def test_unsafe_pdf_link_from_landing_blocked(monkeypatch, install_pdf_transport_get):
    def fake_get(url, **kwargs):
        return FakeResponse(
            url="https://example.test/landing",
            content=b'<html><iframe src="https://sci-hub.se/pdf"></iframe></html>',
            content_type="text/html",
        )

    install_pdf_transport_get(fake_get)
    resolver = OriginalLinkResolver()
    ctx = _ctx({"links": {"url": "https://example.test/landing"}})

    result = resolver.resolve(ctx)

    assert result.success is False
    assert "unsafe" in result.error


# ── 7. direct candidate fails, landing candidate succeeds ─────────────

def test_direct_fails_landing_succeeds(monkeypatch, install_pdf_transport_get):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "direct" in url:
            raise RuntimeError("connection refused")
        if "landing" in url:
            return FakeResponse(
                url="https://example.test/landing",
                content=b'<html><a href="/real.pdf">PDF</a></html>',
                content_type="text/html",
            )
        return FakeResponse(url=url, content=b"%PDF real", content_type="application/pdf")

    install_pdf_transport_get(fake_get)
    resolver = OriginalLinkResolver()
    ctx = _ctx({"links": {"pdf_url": "https://example.test/direct.pdf",
                          "url": "https://example.test/landing"}})

    result = resolver.resolve(ctx)

    assert result.success is True
    assert result.is_direct_pdf is False


# ── 8. safe pdf_url redirects to unsafe host → blocked ───────────────

def test_direct_pdf_redirect_to_unsafe_host_blocked(monkeypatch, install_pdf_transport_get):
    """A safe pdf_url that redirects to an unsafe host (sci-hub) must fail."""
    def fake_get(url, **kwargs):
        # request URL is safe, but the final URL after redirect is unsafe
        return FakeResponse(
            url="https://sci-hub.se/final.pdf",
            content=b"%PDF fake",
            content_type="application/pdf",
        )

    install_pdf_transport_get(fake_get)
    resolver = OriginalLinkResolver()
    ctx = _ctx({"links": {"pdf_url": "https://example.test/paper.pdf"}})

    result = resolver.resolve(ctx)

    assert result.success is False
    assert "unsafe final URL" in (result.error or "")
