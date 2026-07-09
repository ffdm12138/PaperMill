"""Regression tests for SciEngineResolver — locks the 10.1360/ DOI chain.

Simulates: SciEngine landing → SciCloud fileNotLogin/view →
viewer iframe → /parse/pdf/ → %PDF.
"""
from __future__ import annotations

from src.fetch.resolvers.base import ResolveContext
from src.fetch.resolvers.sciengine_resolver import SciEngineResolver


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
    return ResolveContext(doi="10.1360/N072016-00215")


# ── Full chain: SciEngine → SciCloud viewer → parse/pdf → %PDF ─────

def test_sciengine_full_chain_success(monkeypatch, install_pdf_transport_get):
    """Mock the full SciEngine → SciCloud fileNotLogin/view → iframe → PDF chain."""
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "sciengine.com/doi/" in url or "doi.org/10.1360" in url:
            return FakeResponse(
                url=url,
                content=(
                    b'<html><a href="https://www.scicloudcenter.com/'
                    b'SSTe/fileNotLogin/view/1078437402390958080">'
                    b'\xe5\x85\x8d\xe8\xb4\xb9\xe8\x8e\xb7\xe5\x8f\x96</a></html>'
                ),
                content_type="text/html",
            )
        if "fileNotLogin/view/" in url:
            return FakeResponse(
                url=url,
                content=(
                    b'<html><iframe src="/SSTe/parse/pdf/test.pdf">'
                    b'</iframe></html>'
                ),
                content_type="text/html",
            )
        if "parse/pdf/" in url or "test.pdf" in url:
            return FakeResponse(
                url=url,
                content=b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF",
                content_type="application/pdf",
            )
        raise AssertionError(f"unexpected URL: {url}")

    install_pdf_transport_get(fake_get)
    resolver = SciEngineResolver()

    result = resolver.resolve(_ctx())

    assert result.success is True, f"expected success, got: {result.error}"
    assert result.resolver == "sciengine_direct"
    assert result.pdf_url != ""
    assert "parse/pdf/" in result.pdf_url or "test.pdf" in result.pdf_url
    assert result.is_direct_pdf is False
    assert result.raw.get("content", b"").startswith(b"%PDF")
    assert len(result.transport_attempts) >= 3
    assert all(a["mode"] == "direct" for a in result.transport_attempts)


# ── Only activates for 10.1360/ DOIs ─────────────────────────────────

def test_sciengine_skips_non_1360_doi():
    """SciEngineResolver must skip DOIs that don't start with 10.1360/."""
    resolver = SciEngineResolver()
    ctx = ResolveContext(doi="10.1000/not-sciengine")

    result = resolver.resolve(ctx)

    assert result.success is False
    assert "not a SciEngine DOI" in result.error


# ── Non-matching DOI handled cleanly ─────────────────────────────────

def test_sciengine_no_pdf_found(monkeypatch, install_pdf_transport_get):
    """When SciEngine landing page has no PDF, return clean failure."""
    def fake_get(url, **kwargs):
        return FakeResponse(
            url=url,
            content=b"<html><body>No access</body></html>",
            content_type="text/html",
        )

    install_pdf_transport_get(fake_get)
    resolver = SciEngineResolver()
    ctx = ResolveContext(doi="10.1360/NoSuchDOI")

    result = resolver.resolve(ctx)

    assert result.success is False
    assert result.resolver == "sciengine_direct"


# ── sciengine_direct appears in auto chain for 10.1360/ DOI ─────────

def test_auto_attempts_include_sciengine_direct_for_10_1360_doi():
    """Verify sciengine_direct is in the auto resolver chain and before
    header_based."""
    import argparse
    import scripts.fetch_pdf_for_paper_raw as m

    args = argparse.Namespace(
        resolver="auto", base_url="", url_template="", timeout=30,
    )
    policy = m._build_policy(args, {})
    names = policy.enabled_resolver_names()
    assert "sciengine_direct" in names
    sci_idx = names.index("sciengine_direct")
    hb_idx = names.index("header_based")
    assert sci_idx < hb_idx, (
        f"sciengine_direct ({sci_idx}) must come before header_based ({hb_idx})"
    )
