from __future__ import annotations

import requests
import pytest

from src.fetch.pdf_transport import (
    PROXY_SKIP_BOT_BLOCKED,
    PdfTransportConfig,
    fetch_url_direct_then_proxy,
    load_pdf_transport_config,
    sanitize_for_persistence,
    sanitize_url_for_persistence,
    sanitize_transport_url,
)


class _Resp:
    def __init__(self, status_code=200, *, url="https://example.test/paper.pdf", content_type="application/pdf", content=b"%PDF-1.7\n"):
        self.status_code = status_code
        self.url = url
        self.headers = {"content-type": content_type}
        self._content = content
        self.closed = False

    def close(self):
        self.closed = True


class _Session:
    queue = []
    instances = []
    calls = []

    def __init__(self):
        self.trust_env = True
        self.closed = False
        _Session.instances.append(self)

    def request(self, method, url, **kwargs):
        _Session.calls.append({"method": method, "url": url, "kwargs": kwargs, "trust_env": self.trust_env})
        item = _Session.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.closed = True


def _install(monkeypatch, *items):
    _Session.queue = list(items)
    _Session.instances = []
    _Session.calls = []
    monkeypatch.setattr(requests, "Session", _Session)


def test_direct_success_no_proxy(monkeypatch):
    resp = _Resp(200)
    _install(monkeypatch, resp)

    with fetch_url_direct_then_proxy("https://example.test/paper.pdf", expected_content="pdf") as transport:
        assert transport.response is resp
        assert [a.mode for a in transport.attempts] == ["direct"]

    assert resp.closed is True
    assert _Session.instances[0].closed is True
    assert _Session.calls[0]["trust_env"] is False


def test_timeout_falls_back_to_proxy(monkeypatch):
    proxy_resp = _Resp(200, url="https://example.test/proxy.pdf")
    _install(monkeypatch, requests.exceptions.ConnectTimeout("timeout"), proxy_resp)
    config = PdfTransportConfig(proxy_url="http://127.0.0.1:7890")

    with fetch_url_direct_then_proxy("https://example.test/paper.pdf", expected_content="pdf", config=config) as transport:
        assert transport.response is proxy_resp
        assert [a.mode for a in transport.attempts] == ["direct", "proxy"]

    assert _Session.calls[1]["kwargs"]["proxies"] == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }


@pytest.mark.parametrize("status", [403, 408, 429, 500, 503])
def test_retryable_direct_status_falls_back(monkeypatch, status):
    direct_resp = _Resp(status)
    proxy_resp = _Resp(200)
    _install(monkeypatch, direct_resp, proxy_resp)

    with fetch_url_direct_then_proxy("https://example.test/paper.pdf", expected_content="pdf") as transport:
        assert transport.response is proxy_resp
        assert [a.status_code for a in transport.attempts] == [status, 200]

    assert direct_resp.closed is True


@pytest.mark.parametrize("status", [401, 404, 407, 410, 451])
def test_terminal_direct_status_does_not_proxy(monkeypatch, status):
    direct_resp = _Resp(status)
    _install(monkeypatch, direct_resp)

    with fetch_url_direct_then_proxy("https://example.test/paper.pdf", expected_content="pdf") as transport:
        assert transport.response is direct_resp
        assert len(transport.attempts) == 1
        assert transport.attempts[0].status_code == status


def test_disabled_fallback_stops_after_direct(monkeypatch):
    _install(monkeypatch, requests.exceptions.ReadTimeout("timeout"))
    config = PdfTransportConfig(proxy_fallback_enabled=False)

    with fetch_url_direct_then_proxy("https://example.test/paper.pdf", expected_content="pdf", config=config) as transport:
        assert transport.response is None
        assert [a.mode for a in transport.attempts] == ["direct"]


def test_malformed_url_does_not_create_proxy_attempt(monkeypatch):
    _install(monkeypatch, requests.exceptions.MissingSchema("bad url"))

    with fetch_url_direct_then_proxy("not-a-url", expected_content="pdf") as transport:
        assert transport.response is None
        assert [a.mode for a in transport.attempts] == ["direct"]
        assert transport.attempts[0].error_type == "MissingSchema"
    assert _Session.instances == []


def test_proxy_status_is_terminal(monkeypatch):
    _install(monkeypatch, _Resp(403), _Resp(407))

    with fetch_url_direct_then_proxy("https://example.test/paper.pdf", expected_content="pdf") as transport:
        assert transport.response.status_code == 407
        assert [a.status_code for a in transport.attempts] == [403, 407]


def test_timeout_precedence(monkeypatch):
    _install(monkeypatch, requests.exceptions.ReadTimeout("timeout"), _Resp(200))
    config = PdfTransportConfig(direct_timeout=11, proxy_timeout=22)

    with fetch_url_direct_then_proxy("https://example.test/paper.pdf", expected_content="pdf", config=config):
        pass
    assert _Session.calls[0]["kwargs"]["timeout"] == 11
    assert _Session.calls[1]["kwargs"]["timeout"] == 22

    _install(monkeypatch, requests.exceptions.ReadTimeout("timeout"), _Resp(200))
    with fetch_url_direct_then_proxy("https://example.test/paper.pdf", expected_content="pdf", direct_timeout=7, proxy_timeout=7, config=config):
        pass
    assert _Session.calls[0]["kwargs"]["timeout"] == 7
    assert _Session.calls[1]["kwargs"]["timeout"] == 7


def test_independent_timeout_overrides(monkeypatch):
    _install(monkeypatch, requests.exceptions.ReadTimeout("timeout"), _Resp(200))
    with fetch_url_direct_then_proxy(
        "https://example.test/paper.pdf",
        expected_content="pdf",
        direct_timeout=3,
        proxy_timeout=9,
    ):
        pass
    assert [_Session.calls[0]["kwargs"]["timeout"], _Session.calls[1]["kwargs"]["timeout"]] == [3, 9]


def test_direct_html_content_mismatch_falls_back_to_proxy(monkeypatch):
    direct = _Resp(200, content_type="text/html", content=b"<html>captcha</html>")
    proxy = _Resp(200, content_type="application/pdf", content=b"%PDF-1.7\n")
    _install(monkeypatch, direct, proxy)
    with fetch_url_direct_then_proxy(
        "https://example.test/paper.pdf", expected_content="pdf"
    ) as transport:
        assert transport.response is proxy
        assert [a.detected_content for a in transport.attempts] == ["html", "pdf"]
        assert transport.attempts[0].reason_code == "challenge_or_html"


def test_proxy_html_content_mismatch_is_typed_failure(monkeypatch):
    _install(
        monkeypatch,
        _Resp(403),
        _Resp(200, content_type="text/html", content=b"<html>login</html>"),
    )
    with fetch_url_direct_then_proxy(
        "https://example.test/paper.pdf", expected_content="pdf"
    ) as transport:
        assert transport.response is None
        assert transport.error == "challenge_or_html"
        assert transport.attempts[-1].error_type == "ContentMismatch"


def test_request_semantics_preserved(monkeypatch):
    _install(monkeypatch, _Resp(403), _Resp(200))
    headers = {"Authorization": "Bearer secret", "User-Agent": "fixed"}
    params = {"download": "1"}

    with fetch_url_direct_then_proxy(
        "https://example.test/paper.pdf",
        expected_content="pdf",
        headers=headers,
        params=params,
        stream=True,
        allow_redirects=False,
        method="GET",
    ):
        pass

    first = _Session.calls[0]["kwargs"]
    second = _Session.calls[1]["kwargs"]
    for kwargs in (first, second):
        assert kwargs["headers"] == headers
        assert kwargs["params"] == params
        assert kwargs["stream"] is True
        assert kwargs["allow_redirects"] is False


def test_sanitize_transport_url_removes_credentials_and_sensitive_query():
    safe = sanitize_transport_url(
        "https://user:pass@example.test:8443/path.pdf?token=a&ok=b&api_key=c#frag"
    )
    assert safe == "https://example.test:8443/path.pdf?ok=b"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/p.pdf?X-Amz-Signature=SECRET&X-Amz-Credential=CRED",
        "https://example.test/p.pdf?sig=SECRET&se=2026&sp=r&sv=1",
        "https://example.test/p.pdf?GoogleAccessId=id&Signature=s&Expires=1",
        "https://example.test/p.pdf?Policy=p&Signature=s&Key-Pair-Id=k",
    ],
)
def test_sanitize_transport_url_removes_common_signed_query_params(url):
    assert "SECRET" not in sanitize_transport_url(url)
    assert sanitize_url_for_persistence(url) == "https://example.test/p.pdf"


def test_sanitize_url_for_persistence_strips_query_userinfo_and_fragment():
    safe = sanitize_url_for_persistence(
        "https://user:pass@example.test/path.pdf?ok=1#frag"
    )
    assert safe == "https://example.test/path.pdf"


def test_sanitize_url_for_persistence_allowlist_keeps_only_allowed_safe_params():
    safe = sanitize_url_for_persistence(
        "https://example.test/path.pdf?ok=1&sig=SECRET&download=1",
        query_policy="allowlist",
        allowed_query_params={"ok", "download", "sig"},
    )
    assert safe == "https://example.test/path.pdf?ok=1&download=1"


def test_sanitize_url_handles_malformed_port_ipv6_and_unicode():
    assert sanitize_url_for_persistence("https://example.test:bad/path.pdf") == ""
    assert sanitize_url_for_persistence("https://[::1]:8443/path.pdf?token=x") == "https://[::1]:8443/path.pdf"
    assert sanitize_url_for_persistence("https://例子.测试/path.pdf?token=x") == "https://例子.测试/path.pdf"


def test_sanitize_for_persistence_redacts_urls_in_every_nested_string():
    data = {
        "title": "https://example.test/title?token=not-a-url-field",
        "pdf_url": "https://user:pass@example.test/p.pdf?token=a",
        "attempts": [
            {
                "request_url": "https://example.test/a.pdf?X-Amz-Signature=s",
                "reason": "failed at https://example.test/a.pdf?X-Amz-Signature=s",
                "redirect_chain": ["https://example.test/b.pdf?sig=s"],
            }
        ],
    }
    safe = sanitize_for_persistence(data)
    assert safe["title"] == "https://example.test/title"
    assert safe["pdf_url"] == "https://example.test/p.pdf"
    assert safe["attempts"][0]["request_url"] == "https://example.test/a.pdf"
    assert safe["attempts"][0]["reason"] == "failed at https://example.test/a.pdf"
    assert safe["attempts"][0]["redirect_chain"] == ["https://example.test/b.pdf"]


@pytest.mark.parametrize("value", ["-1", "0", "nan", "inf", "bad"])
def test_invalid_timeout_env_values_fall_back(value):
    config = load_pdf_transport_config(env={"MINERU_PDF_DIRECT_TIMEOUT": value})
    assert config.direct_timeout == 30.0


def test_valid_timeout_env_value_used():
    config = load_pdf_transport_config(env={"MINERU_PDF_DIRECT_TIMEOUT": "12.5"})
    assert config.direct_timeout == 12.5


def test_config_empty_env_does_not_read_process_env(monkeypatch):
    monkeypatch.setenv("MINERU_PDF_PROXY_URL", "http://real-env-proxy")
    config = load_pdf_transport_config(env={})
    assert config.proxy_url == "http://127.0.0.1:7890"


# ── proxy retry is skipped when it provably cannot help ────────────────
#
# A 403 from a host that scores requests by originating network is a verdict
# on the egress, not the request. Measured: 551 direct 403s produced 548
# proxy 403s and zero successes.

def test_403_from_bot_blocked_host_skips_the_proxy_retry(monkeypatch):
    direct_resp = _Resp(403, url="https://www.mdpi.com/2071-1050/18/3/1645/pdf",
                        content_type="text/html", content=b"<HTML>Access Denied")
    _install(monkeypatch, direct_resp)

    with fetch_url_direct_then_proxy(
        "https://www.mdpi.com/2071-1050/18/3/1645/pdf", expected_content="pdf"
    ) as transport:
        assert transport.response is None
        assert [a.mode for a in transport.attempts] == ["direct"]
        assert transport.error == PROXY_SKIP_BOT_BLOCKED

    assert len(_Session.calls) == 1, "no second request may be issued"


def test_403_from_an_ordinary_host_still_retries_through_the_proxy(monkeypatch):
    direct_resp = _Resp(403, url="https://acp.copernicus.org/articles/1/1.pdf")
    proxy_resp = _Resp(200, url="https://acp.copernicus.org/articles/1/1.pdf")
    _install(monkeypatch, direct_resp, proxy_resp)

    with fetch_url_direct_then_proxy(
        "https://acp.copernicus.org/articles/1/1.pdf", expected_content="pdf"
    ) as transport:
        assert transport.response is proxy_resp
        assert [a.mode for a in transport.attempts] == ["direct", "proxy"]


def test_blocked_host_still_retries_on_connection_error(monkeypatch):
    """Only the 403 verdict is futile. Transport-level failures are not: the
    proxy does convert those into successes, so the fallback must stay."""
    proxy_resp = _Resp(200, url="https://www.mdpi.com/x/pdf")
    _install(monkeypatch, requests.exceptions.ConnectTimeout("timeout"), proxy_resp)

    with fetch_url_direct_then_proxy("https://www.mdpi.com/x/pdf", expected_content="pdf") as transport:
        assert transport.response is proxy_resp
        assert [a.mode for a in transport.attempts] == ["direct", "proxy"]


def test_403_redirected_to_a_blocked_host_is_recognized(monkeypatch):
    """doi.org itself is never blocked; the publisher it redirects to is."""
    direct_resp = _Resp(403, url="https://www.mdpi.com/2071-1050/18/3/1645/pdf",
                        content_type="text/html", content=b"<HTML>")
    _install(monkeypatch, direct_resp)

    with fetch_url_direct_then_proxy("https://doi.org/10.3390/su18031645", expected_content="pdf") as transport:
        assert transport.response is None
        assert transport.error == PROXY_SKIP_BOT_BLOCKED
