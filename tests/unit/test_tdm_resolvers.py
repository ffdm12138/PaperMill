"""Tests for TDM resolver unsafe-redirect blocking."""
from __future__ import annotations

import pytest

from src.fetch.resolvers.base import ResolveContext
from src.fetch.resolvers.tdm_resolvers import WileyTdmResolver


class FakeResponse:
    def __init__(self, *, url: str, content: bytes = b"", content_type: str = "",
                 status_code: int = 200, headers: dict | None = None):
        self.url = url
        self.content = content
        self.headers = headers or {"Content-Type": content_type}
        self.status_code = status_code
        self.text = content.decode("utf-8", errors="ignore")

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=1024):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]


def _ctx() -> ResolveContext:
    return ResolveContext(doi="10.1002/test", access_policy=None)


def test_wiley_302_redirect_to_unsafe_location_blocked(monkeypatch):
    """Wiley 302 whose Location header points at an unsafe host must fail."""
    responses = []

    def fake_get(url, **kwargs):
        if "api.wiley.com" in url:
            # 302 redirect to an unsafe host
            return FakeResponse(
                url=url, content=b"", status_code=302,
                headers={"Content-Type": "text/html", "Location": "https://sci-hub.se/wiley.pdf"},
            )
        # would be the unsafe redirect target; must never be reached
        responses.append(url)
        return FakeResponse(url=url, content=b"%PDF fake", content_type="application/pdf")

    monkeypatch.setattr("src.fetch.resolvers.tdm_resolvers.requests.get", fake_get)
    resolver = WileyTdmResolver()
    result = resolver.resolve(_ctx())

    assert result.success is False
    assert "unsafe" in (result.error or "")
    # the unsafe redirect target must NOT have been fetched
    assert not responses


def test_wiley_200_final_url_unsafe_blocked(monkeypatch):
    """Wiley 200 response whose final URL (after redirect) is unsafe must fail."""
    def fake_get(url, **kwargs):
        return FakeResponse(
            url="https://z-lib.org/final.pdf",
            content=b"%PDF fake",
            content_type="application/pdf",
        )

    monkeypatch.setattr("src.fetch.resolvers.tdm_resolvers.requests.get", fake_get)
    resolver = WileyTdmResolver()
    result = resolver.resolve(_ctx())

    assert result.success is False
    assert "unsafe final URL" in (result.error or "")
