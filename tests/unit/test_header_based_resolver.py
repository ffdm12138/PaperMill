"""Header-based DOI resolver unit tests — all network access is monkeypatched."""
from __future__ import annotations

import pytest

from src.fetch.access_policy import AccessMode, AccessPolicy
from src.fetch.resolvers.base import ResolveContext
from src.fetch.resolvers.header_based_resolver import FIXED_USER_AGENT, HeaderBasedDoiResolver


pytestmark = pytest.mark.unit


class FakeResponse:
    def __init__(self, *, url: str, content: bytes, content_type: str,
                 status_code: int = 200):
        self.url = url
        self.content = content
        self.headers = {"content-type": content_type}
        self.status_code = status_code
        self.text = content.decode("utf-8", errors="ignore")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1024):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]


def _ctx() -> ResolveContext:
    return ResolveContext(
        doi="10.1000/test",
        access_policy=AccessPolicy(mode=AccessMode.CUSTOM),
    )


# ── Direct PDF ─────────────────────────────────────────────────────────

def test_direct_pdf_uses_fixed_user_agent(monkeypatch, install_pdf_transport_get):
    seen = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs["headers"]
        return FakeResponse(url=url, content=b"%PDF direct",
                            content_type="application/pdf")

    install_pdf_transport_get(fake_get)
    resolver = HeaderBasedDoiResolver(
        url_template="https://example.test/fetch?doi={doi}",
        headers={"Cookie": "secret"},
    )

    result = resolver.resolve(_ctx())

    assert result.success is True
    assert result.raw["content"] == b"%PDF direct"
    assert seen["headers"]["User-Agent"] == FIXED_USER_AGENT
    assert seen["headers"]["Cookie"] == "secret"
    assert result.metadata["headers_masked"] is True
    assert "Cookie" in result.metadata["header_keys"]


# ── Landing page → PDF link ────────────────────────────────────────────

def test_html_landing_page_pdf_link_is_downloaded(monkeypatch, install_pdf_transport_get):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "doi" in url:
            return FakeResponse(
                url="https://example.test/landing",
                content=b'<html><a href="/paper.pdf">PDF</a></html>',
                content_type="text/html",
            )
        return FakeResponse(url=url, content=b"%PDF linked",
                            content_type="application/pdf")

    install_pdf_transport_get(fake_get)
    resolver = HeaderBasedDoiResolver(base_url="https://example.test/doi/")

    result = resolver.resolve(_ctx())

    assert result.success is True
    assert calls[0] == "https://example.test/doi/10.1000/test"
    assert result.is_direct_pdf is False


# ── Non-PDF rejection ──────────────────────────────────────────────────

def test_rejects_non_pdf_response(monkeypatch, install_pdf_transport_get):
    def fake_get(url, **kwargs):
        return FakeResponse(url=url, content=b"not pdf", content_type="text/plain")

    install_pdf_transport_get(fake_get)
    resolver = HeaderBasedDoiResolver(base_url="https://example.test/doi/")

    result = resolver.resolve(_ctx())

    assert result.success is False
    assert "PDF" in result.error or "candidates" in result.error or result.error


# ── Unsafe host blocked before network ─────────────────────────────────

def test_blocks_unsafe_host_without_network(monkeypatch, install_pdf_transport_get):
    def fail_get(*args, **kwargs):
        raise AssertionError("unsafe host must be blocked before network")

    install_pdf_transport_get(fail_get)
    resolver = HeaderBasedDoiResolver(base_url="https://sci-hub.se/")

    result = resolver.resolve(_ctx())

    assert result.success is False
    assert "unsafe source blocked" in result.error


# ── Invalid PDF magic ──────────────────────────────────────────────────

def test_invalid_pdf_magic_rejected(monkeypatch, install_pdf_transport_get):
    def fake_get(url, **kwargs):
        return FakeResponse(url=url, content=b"not a pdf",
                            content_type="application/pdf")

    install_pdf_transport_get(fake_get)
    resolver = HeaderBasedDoiResolver(base_url="https://example.test/doi/")

    result = resolver.resolve(_ctx())

    assert result.success is False
    assert "valid PDF" in result.error


# ── Redirect to unsafe host blocked ────────────────────────────────────

def test_redirect_to_unsafe_host_blocked(monkeypatch, install_pdf_transport_get):
    def fake_get(url, **kwargs):
        return FakeResponse(
            url="https://libgen.is/final.pdf",
            content=b"%PDF fake",
            content_type="application/pdf",
        )

    install_pdf_transport_get(fake_get)
    resolver = HeaderBasedDoiResolver(base_url="https://example.test/doi/")

    result = resolver.resolve(_ctx())

    assert result.success is False
    assert "unsafe final URL" in (result.error or "")


# ── Defaults to doi.org ────────────────────────────────────────────────

def test_defaults_to_doi_org_without_base_or_template(monkeypatch, install_pdf_transport_get):
    seen_url = {}

    def fake_get(url, **kwargs):
        seen_url["url"] = url
        return FakeResponse(url=url, content=b"%PDF default",
                            content_type="application/pdf")

    install_pdf_transport_get(fake_get)
    resolver = HeaderBasedDoiResolver()

    result = resolver.resolve(_ctx())
    assert result.success is True
    assert seen_url["url"] == "https://doi.org/10.1000/test"
    assert result.raw["content"] == b"%PDF default"


# ── URL template DOI placeholders (parametrized) ───────────────────────

@pytest.mark.parametrize("template, expected_url", [
    ("https://e.test/doi/{doi_path}", "https://e.test/doi/10.1000/test"),
    ("https://e.test/fetch?doi={doi_query}",
     "https://e.test/fetch?doi=10.1000%2Ftest"),
])
def test_url_template_doi_placeholders(monkeypatch, install_pdf_transport_get, template, expected_url):
    seen_url = {}

    def fake_get(url, **kwargs):
        seen_url["url"] = url
        return FakeResponse(url=url, content=b"%PDF", content_type="application/pdf")

    install_pdf_transport_get(fake_get)
    resolver = HeaderBasedDoiResolver(url_template=template)

    result = resolver.resolve(_ctx())
    assert result.success is True
    assert seen_url["url"] == expected_url
