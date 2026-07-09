"""Content-first ordering tests for src.fetch.fetch_pipeline.fetch_pdf.

Verifies that when a resolver returns bytes in ``raw["content"]`` (e.g. the
header_based resolver that already downloaded with authorized headers), the
pipeline uses those bytes directly and does NOT re-download via ``pdf_url``
with a headerless request.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.fetch import fetch_pipeline
from src.fetch.fetch_pipeline import fetch_pdf
from src.fetch.models import FetchResult
from src.fetch.resolvers.base import PdfResolver, ResolveContext


class _StaticResolver(PdfResolver):
    """Resolver that returns a canned FetchResult."""

    name = "static_test"
    access_modes = ("oa_only", "institutional", "custom")

    def __init__(self, result: FetchResult):
        self._result = result

    def resolve(self, context: ResolveContext) -> FetchResult:
        return self._result


def _install_static(monkeypatch, result: FetchResult):
    monkeypatch.setattr(
        fetch_pipeline,
        "_build_resolvers",
        lambda policy: [_StaticResolver(result)],
    )


class _TransportCtx:
    def __init__(self, response=None, *, error: str = "", attempts=None):
        self.response = response
        self.error = error
        self.safe_attempts = list(attempts or [])

    def __enter__(self):
        if self.response is not None and not hasattr(self.response, "status_code"):
            self.response.status_code = 200
        return self

    def __exit__(self, *args):
        close = getattr(self.response, "close", None)
        if callable(close):
            close()
        return False


def _patch_download_transport(monkeypatch, response=None, *, error: str = "", attempts=None, capture=None):
    def _fake_transport(url, **kwargs):
        if capture is not None:
            capture["url"] = url
            capture["kwargs"] = kwargs
        return _TransportCtx(response, error=error, attempts=attempts)

    monkeypatch.setattr(fetch_pipeline, "fetch_url_direct_then_proxy", _fake_transport)


def test_content_used_when_raw_content_present(monkeypatch, tmp_path):
    download_calls = {"count": 0}

    def _boom_download(url, target, *, timeout=60):
        download_calls["count"] += 1
        raise AssertionError("must not re-download via pdf_url when raw content present")

    monkeypatch.setattr(fetch_pipeline, "_download_pdf", _boom_download)

    result = FetchResult(
        doi="10.1000/test",
        success=True,
        source="static_test",
        resolver="static_test",
        pdf_url="http://example.test/should-not-be-used.pdf",
        raw={"content": b"%PDF-bytes-from-resolver"},
    )
    _install_static(monkeypatch, result)

    out = fetch_pdf("10.1000/test", output_root=tmp_path)

    assert out.success is True
    assert download_calls["count"] == 0
    assert Path(out.output_path).read_bytes() == b"%PDF-bytes-from-resolver"
    assert out.sha256
    # content is consumed / popped after writing
    assert "content" not in result.raw


def test_pdf_url_used_when_no_content(monkeypatch, tmp_path):
    monkeypatch.setattr(
        fetch_pipeline,
        "_download_pdf",
        lambda url, target, *, timeout=60: (target, "sha-pdf-url"),
    )

    result = FetchResult(
        doi="10.1000/test",
        success=True,
        source="static_test",
        resolver="static_test",
        pdf_url="http://example.test/paper.pdf",
        raw={},
    )
    _install_static(monkeypatch, result)

    out = fetch_pdf("10.1000/test", output_root=tmp_path)

    assert out.success is True
    assert Path(out.output_path).as_posix().endswith(".pdf")
    assert out.sha256 == "sha-pdf-url"


def test_output_path_used_when_neither(monkeypatch, tmp_path):
    source_pdf = tmp_path / "source.pdf"
    source_pdf.write_bytes(b"%PDF-copied")

    result = FetchResult(
        doi="10.1000/test",
        success=True,
        source="static_test",
        resolver="static_test",
        output_path=str(source_pdf),
        raw={},
    )
    _install_static(monkeypatch, result)

    out = fetch_pdf("10.1000/test", output_root=tmp_path / "out")

    assert out.success is True
    assert Path(out.output_path).read_bytes() == b"%PDF-copied"


def test_header_based_does_not_double_download(monkeypatch, tmp_path):
    """Regression: header_based returns content + pdf_url; pipeline must use
    the already-fetched bytes and never call the headerless _download_pdf."""
    download_calls = {"count": 0}

    def _boom_download(url, target, *, timeout=60):
        download_calls["count"] += 1
        raise AssertionError("header_based content must be used, not re-downloaded")

    monkeypatch.setattr(fetch_pipeline, "_download_pdf", _boom_download)

    # A header_based-style result: both pdf_url and raw["content"] set.
    result = FetchResult(
        doi="10.1000/test",
        success=True,
        source="header_based",
        resolver="header_based",
        pdf_url="http://publisher.test/behind-cookie-wall.pdf",
        access_mode="custom",
        access_status="authorized_header",
        is_direct_pdf=False,
        raw={"content": b"%PDF-fetched-with-headers"},
    )
    _install_static(monkeypatch, result)

    out = fetch_pdf("10.1000/test", output_root=tmp_path)

    assert out.success is True
    assert download_calls["count"] == 0
    assert Path(out.output_path).read_bytes() == b"%PDF-fetched-with-headers"


def test_success_result_has_attempts(monkeypatch, tmp_path):
    """成功结果的 attempts 应记录成功的 resolver。"""
    monkeypatch.setattr(
        fetch_pipeline,
        "_download_pdf",
        lambda url, target, *, timeout=60: (target, "sha-pdf-url"),
    )
    result = FetchResult(
        doi="10.1000/test",
        success=True,
        source="static_test",
        resolver="static_test",
        pdf_url="http://example.test/paper.pdf",
        raw={},
    )
    _install_static(monkeypatch, result)

    out = fetch_pdf("10.1000/test", output_root=tmp_path)

    assert out.success is True
    assert len(out.attempts) == 1
    assert out.attempts[0]["resolver"] == "static_test"
    assert out.attempts[0]["status"] == "success"


def test_failure_result_has_full_attempts(monkeypatch, tmp_path):
    """失败结果的 attempts 应记录所有尝试过的 resolver。"""

    class _FailResolver(PdfResolver):
        name = "fail_a"
        access_modes = ("oa_only",)

        def resolve(self, context):
            return FetchResult(doi=context.doi, source="fail_a", resolver="fail_a",
                               error="no PDF found")

    class _FailResolverB(PdfResolver):
        name = "fail_b"
        access_modes = ("oa_only",)

        def resolve(self, context):
            return FetchResult(doi=context.doi, source="fail_b", resolver="fail_b",
                               error="also no PDF")

    monkeypatch.setattr(
        fetch_pipeline,
        "_build_resolvers",
        lambda policy: [_FailResolver(), _FailResolverB()],
    )

    out = fetch_pdf("10.1000/test", output_root=tmp_path)

    assert out.success is False
    assert len(out.attempts) == 2
    assert out.attempts[0]["resolver"] == "fail_a"
    assert out.attempts[0]["status"] == "failed"
    assert out.attempts[0]["reason"] == "no PDF found"
    assert out.attempts[1]["resolver"] == "fail_b"
    assert out.attempts[1]["status"] == "failed"


def test_transport_attempts_preserved_when_later_resolver_succeeds(monkeypatch, tmp_path):
    class _FailResolver(PdfResolver):
        name = "fail_with_transport"
        access_modes = ("oa_only",)

        def resolve(self, context):
            return FetchResult(
                doi=context.doi,
                source=self.name,
                resolver=self.name,
                error="direct failed",
                transport_attempts=[{"mode": "direct", "request_url": "https://a.test/fail.pdf"}],
            )

    class _SuccessResolver(PdfResolver):
        name = "success_with_content"
        access_modes = ("oa_only",)

        def resolve(self, context):
            return FetchResult(
                doi=context.doi,
                success=True,
                source=self.name,
                resolver=self.name,
                raw={"content": b"%PDF-success"},
                transport_attempts=[{"mode": "direct", "request_url": "https://b.test/ok.pdf"}],
            )

    monkeypatch.setattr(
        fetch_pipeline,
        "_build_resolvers",
        lambda policy: [_FailResolver(), _SuccessResolver()],
    )

    out = fetch_pdf("10.1000/test", output_root=tmp_path)

    assert out.success is True
    assert [a["resolver"] for a in out.attempts] == ["fail_with_transport", "success_with_content"]
    assert [a["request_url"] for a in out.transport_attempts] == [
        "https://a.test/fail.pdf",
        "https://b.test/ok.pdf",
    ]


def test_not_configured_resolvers_on_failure_when_not_configured(monkeypatch, tmp_path):
    """--resolver auto without header config: header_based is NOT executed, so
    attempts must NOT contain a header_based entry; instead the top-level
    not_configured_resolvers lists it as a report marker."""
    from src.fetch.access_policy import AccessMode, AccessPolicy

    class _FailResolver(PdfResolver):
        name = "fail_test"
        access_modes = ("oa_only",)

        def resolve(self, context):
            return FetchResult(doi=context.doi, error="no PDF")

    monkeypatch.setattr(
        fetch_pipeline,
        "_build_resolvers",
        lambda policy: [_FailResolver()],
    )

    policy = AccessPolicy(
        mode=AccessMode.OA_ONLY,
        extra={"not_configured_resolvers": ["header_based"]},
    )
    out = fetch_pdf("10.1000/test", output_root=tmp_path, access_policy=policy)

    assert out.success is False
    # attempts only record resolvers that actually ran; header_based never ran
    assert len(out.attempts) == 1
    assert out.attempts[0]["resolver"] == "fail_test"
    assert out.attempts[0]["status"] == "failed"
    assert not any(a["resolver"] == "header_based" for a in out.attempts)
    # the not-configured marker surfaces at the top level, not as an attempt
    assert out.not_configured_resolvers == ["header_based"]


def test_success_does_not_add_not_configured_marker(monkeypatch, tmp_path):
    """Success must not add header_based to not_configured_resolvers."""
    from src.fetch.access_policy import AccessMode, AccessPolicy

    result = FetchResult(
        doi="10.1000/test",
        success=True,
        source="static_test",
        resolver="static_test",
        pdf_url="http://example.test/paper.pdf",
        raw={},
    )
    _install_static(monkeypatch, result)
    monkeypatch.setattr(
        fetch_pipeline,
        "_download_pdf",
        lambda url, target, *, timeout=60: (target, "sha"),
    )

    policy = AccessPolicy(
        mode=AccessMode.OA_ONLY,
        extra={"not_configured_resolvers": ["header_based"]},
    )
    out = fetch_pdf("10.1000/test", output_root=tmp_path, access_policy=policy)

    assert out.success is True
    assert len(out.attempts) == 1
    assert out.attempts[0]["resolver"] == "static_test"
    assert out.attempts[0]["status"] == "success"
    # no header_based attempt even though not_configured_resolvers is set
    assert not any(a["resolver"] == "header_based" for a in out.attempts)
    assert out.not_configured_resolvers == ["header_based"]


def test_no_not_configured_marker_without_extra(monkeypatch, tmp_path):
    """Without not_configured_resolvers in policy.extra, the marker stays empty
    even on failure."""
    from src.fetch.access_policy import AccessMode, AccessPolicy

    class _FailResolver(PdfResolver):
        name = "fail_test"
        access_modes = ("oa_only",)

        def resolve(self, context):
            return FetchResult(doi=context.doi, error="no PDF")

    monkeypatch.setattr(
        fetch_pipeline,
        "_build_resolvers",
        lambda policy: [_FailResolver()],
    )

    policy = AccessPolicy(mode=AccessMode.OA_ONLY)
    out = fetch_pdf("10.1000/test", output_root=tmp_path, access_policy=policy)

    assert out.success is False
    assert len(out.attempts) == 1
    assert out.attempts[0]["resolver"] == "fail_test"
    assert not any(a["resolver"] == "header_based" for a in out.attempts)
    assert out.not_configured_resolvers == []


def test_pipeline_blocks_unsafe_pdf_url_from_resolver(monkeypatch, tmp_path):
    """A resolver returning an unsafe pdf_url (e.g. sci-hub) must NOT be
    downloaded; the pipeline records the failure and never calls _download_pdf."""
    download_calls = {"count": 0}

    def _boom_download(url, target, *, timeout=60):
        download_calls["count"] += 1
        raise AssertionError(f"must not download unsafe url: {url}")

    monkeypatch.setattr(fetch_pipeline, "_download_pdf", _boom_download)

    # resolver returns a sci-hub pdf_url as if from an OA API
    unsafe_result = FetchResult(
        doi="10.1000/test",
        success=True,
        source="fake_oa",
        resolver="fake_oa",
        pdf_url="https://sci-hub.se/10.1000/test.pdf",
        raw={},
    )
    _install_static(monkeypatch, unsafe_result)

    out = fetch_pdf("10.1000/test", output_root=tmp_path)

    assert out.success is False
    assert download_calls["count"] == 0
    # the failure is recorded as an attempt with unsafe-blocked reason.
    # _install_static wraps the result in _StaticResolver (name='static_test'),
    # so the attempt resolver is static_test, not fake_oa.
    assert any(
        a["resolver"] == "static_test" and "unsafe" in (a["reason"] or "")
        for a in out.attempts
    )


def test_download_pdf_blocks_unsafe_url_directly(monkeypatch, tmp_path):
    """_download_pdf itself must refuse an unsafe url before any network call."""
    import requests as _requests

    def _no_network(*args, **kwargs):
        raise AssertionError("no network call allowed for unsafe url")

    monkeypatch.setattr(_requests, "get", _no_network)

    target = tmp_path / "out.pdf"
    with pytest.raises(ValueError, match="unsafe source blocked"):
        fetch_pipeline._download_pdf("https://libgen.is/book.pdf", target)


def test_download_pdf_uses_pdf_transport(monkeypatch, tmp_path):
    """_download_pdf uses the direct-first PDF transport helper."""
    captured = {}

    class _FakeResp:
        headers = {"content-type": "application/pdf"}
        url = "http://example.test/paper.pdf"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=65536):
            yield b"%PDF-1.4 fake body"

    _patch_download_transport(monkeypatch, _FakeResp(), capture=captured)

    target = tmp_path / "out.pdf"
    fetch_pipeline._download_pdf("http://example.test/paper.pdf", target)
    assert captured["kwargs"]["expected_content"] == "pdf"
    assert captured["kwargs"]["stream"] is True


def test_oa_helper_passes_proxies(monkeypatch):
    """fetch_openalex.resolve_openalex_pdf must pass proxies to requests.get."""
    import src.fetch.fetch_openalex as oa_mod
    import src.fetch.proxy as proxy_mod

    monkeypatch.setattr(proxy_mod, "FETCH_PROXY", "http://127.0.0.1:7890", raising=False)

    # Isolate from real system env vars
    from src.services.openalex_credentials import OpenAlexCredentials
    monkeypatch.setattr(
        oa_mod, "load_openalex_credentials",
        lambda *a, **k: OpenAlexCredentials(email=None, api_key=None),
    )

    captured = {}

    class _FakeResp:
        headers = {"content-type": "application/json"}

        def raise_for_status(self):
            pass

        def json(self):
            return {"results": []}

    def _fake_get(url, *, params=None, headers=None, timeout=20, proxies=None, **kw):
        captured["proxies"] = proxies
        return _FakeResp()

    monkeypatch.setattr(oa_mod.requests, "get", _fake_get)
    oa_mod.resolve_openalex_pdf("10.1000/test")
    assert captured["proxies"] == {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}


def test_limit_content_rejects_oversize_content_length_without_reading_body():
    """limit_content must fail on an oversized Content-Length header BEFORE
    reading any of the body — so a huge PDF/error page is never loaded."""
    from src.fetch.resolvers.url_safety import limit_content
    from config import settings

    body_read = {"count": 0}

    class _OversizeResponse:
        headers = {"Content-Length": str(settings.MINERU_FETCH_MAX_BYTES + 1)}
        url = "http://example.test/huge.pdf"

        def iter_content(self, chunk_size=65536):
            body_read["count"] += 1
            raise AssertionError("body must not be read when Content-Length exceeds limit")

    with pytest.raises(ValueError, match="exceeds MINERU_FETCH_MAX_BYTES"):
        limit_content(_OversizeResponse())
    assert body_read["count"] == 0


def test_limit_content_streams_and_aborts_on_running_total():
    """When Content-Length is absent, limit_content streams chunks and aborts
    as soon as the running total crosses the limit."""
    from src.fetch.resolvers import url_safety
    from config import settings

    # Lower the limit so the test is cheap.
    monkeypatch_limit = settings.MINERU_FETCH_MAX_BYTES
    orig = url_safety.MINERU_FETCH_MAX_BYTES
    url_safety.MINERU_FETCH_MAX_BYTES = 100
    try:
        class _StreamingResponse:
            headers = {}  # no Content-Length
            url = "http://example.test/big.pdf"

            def iter_content(self, chunk_size=65536):
                yield b"x" * 60
                yield b"x" * 60  # total 120 > 100, must abort here

        with pytest.raises(ValueError, match="exceeds MINERU_FETCH_MAX_BYTES"):
            url_safety.limit_content(_StreamingResponse())
    finally:
        url_safety.MINERU_FETCH_MAX_BYTES = orig
    _ = monkeypatch_limit  # keep reference


def test_download_pdf_blocks_unsafe_final_url_after_redirect(monkeypatch, tmp_path):
    """_download_pdf must reject a response whose final URL (after redirect)
    is an unsafe host, even though the original URL was safe. No file written."""
    target = tmp_path / "out.pdf"

    class _RedirectedResponse:
        url = "https://sci-hub.se/final.pdf"  # redirect landed here
        headers = {"content-type": "application/pdf"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=65536):
            yield b"%PDF-1.4 should not be written"

    _patch_download_transport(monkeypatch, _RedirectedResponse())

    with pytest.raises(ValueError, match="unsafe final URL blocked"):
        fetch_pipeline._download_pdf("https://example.com/start", target)
    assert not target.exists()


def test_download_pdf_rejects_html_body_with_pdf_content_type(monkeypatch, tmp_path):
    """Content-Type: application/pdf but body is HTML → must be rejected by
    the %PDF magic byte check; no file written."""
    target = tmp_path / "out.pdf"

    class _HtmlResponse:
        url = "https://example.test/paper.pdf"
        headers = {"content-type": "application/pdf"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=65536):
            yield b"<html><body>not a pdf</body></html>"

    _patch_download_transport(monkeypatch, _HtmlResponse())

    with pytest.raises(ValueError, match="not a valid PDF"):
        fetch_pipeline._download_pdf("https://example.test/paper.pdf", target)
    assert not target.exists()


def test_download_pdf_rejects_non_pdf_body_with_pdf_url_suffix(monkeypatch, tmp_path):
    """URL ends with .pdf but body is not %PDF → must be rejected."""
    target = tmp_path / "out.pdf"

    class _FakeResponse:
        url = "https://example.test/paper.pdf"
        headers = {"content-type": "application/octet-stream"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=65536):
            yield b"PK\x03\x04 zip archive not a pdf"

    _patch_download_transport(monkeypatch, _FakeResponse())

    with pytest.raises(ValueError, match="not a valid PDF"):
        fetch_pipeline._download_pdf("https://example.test/paper.pdf", target)
    assert not target.exists()


def test_download_pdf_writes_valid_pdf_body(monkeypatch, tmp_path):
    """A legitimate %PDF body must be written successfully."""
    target = tmp_path / "out.pdf"
    body = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"

    class _ValidResponse:
        url = "https://example.test/paper.pdf"
        headers = {"content-type": "application/pdf"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=65536):
            yield body

    _patch_download_transport(monkeypatch, _ValidResponse())

    path, sha = fetch_pipeline._download_pdf("https://example.test/paper.pdf", target)
    assert path == target
    assert target.read_bytes() == body
    assert len(sha) == 64


def test_download_pdf_rejects_empty_response(monkeypatch, tmp_path):
    """iter_content yields no non-empty chunks → must reject as empty PDF."""

    class _EmptyResponse:
        url = "https://example.test/paper.pdf"
        headers = {"content-type": "application/pdf"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=65536):
            # no chunks at all
            yield from ()

    _patch_download_transport(monkeypatch, _EmptyResponse())

    target = tmp_path / "out.pdf"
    with pytest.raises(ValueError, match="empty PDF response"):
        fetch_pipeline._download_pdf("https://example.test/paper.pdf", target)
    assert not target.exists()


def test_download_pdf_accepts_split_pdf_magic(monkeypatch, tmp_path):
    """%PDF magic split across two chunks must still be accepted."""
    body = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"

    class _SplitResponse:
        url = "https://example.test/paper.pdf"
        headers = {"content-type": "application/pdf"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=65536):
            yield b"%P"
            yield b"DF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"

    _patch_download_transport(monkeypatch, _SplitResponse())

    target = tmp_path / "out.pdf"
    path, sha = fetch_pipeline._download_pdf("https://example.test/paper.pdf", target)
    assert path == target
    assert target.read_bytes() == body
    assert len(sha) == 64


def test_download_pdf_rejects_split_non_pdf_magic(monkeypatch, tmp_path):
    """Non-%PDF magic split across chunks must still be rejected."""

    class _SplitNonPdf:
        url = "https://example.test/paper.pdf"
        headers = {"content-type": "application/pdf"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=65536):
            yield b"<h"
            yield b"tml><body>not pdf</body></html>"

    _patch_download_transport(monkeypatch, _SplitNonPdf())

    target = tmp_path / "out.pdf"
    with pytest.raises(ValueError, match="not a valid PDF"):
        fetch_pipeline._download_pdf("https://example.test/paper.pdf", target)
    assert not target.exists()


def test_copy_pdf_rejects_html_source_file(monkeypatch, tmp_path):
    """_copy_pdf must reject a source file whose content is HTML, not %PDF."""
    src = tmp_path / "fake.pdf"
    src.write_bytes(b"<html><body>not a pdf</body></html>")
    target = tmp_path / "out.pdf"

    with pytest.raises(ValueError, match="not a valid PDF"):
        fetch_pipeline._copy_pdf(src, target)
    assert not target.exists()


def test_copy_pdf_rejects_empty_source_file(monkeypatch, tmp_path):
    """_copy_pdf must reject an empty source file."""
    src = tmp_path / "empty.pdf"
    src.write_bytes(b"")
    target = tmp_path / "out.pdf"

    with pytest.raises(ValueError, match="empty PDF response"):
        fetch_pipeline._copy_pdf(src, target)
    assert not target.exists()


def test_copy_pdf_copies_valid_pdf(monkeypatch, tmp_path):
    """_copy_pdf must accept and copy a legitimate %PDF source file."""
    body = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
    src = tmp_path / "valid.pdf"
    src.write_bytes(body)
    target = tmp_path / "out.pdf"

    path, sha = fetch_pipeline._copy_pdf(src, target)
    assert path == target
    assert target.read_bytes() == body
    assert len(sha) == 64


# ── Fetch OpenAlex credential tests ─────────────────────

def test_fetch_openalex_uses_centralized_credentials(monkeypatch):
    """With credentials set, resolve_openalex_pdf must pass them as params/headers."""
    import src.fetch.fetch_openalex as oa_mod
    from src.services.openalex_credentials import OpenAlexCredentials

    monkeypatch.setattr(
        oa_mod, "load_openalex_credentials",
        lambda *a, **k: OpenAlexCredentials(email="fetch@test.org", api_key="fetch-test-key-abc"),
    )

    captured = {}

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": []}

    def _fake_get(url, *, params=None, headers=None, timeout=20, proxies=None, **kw):
        captured["params"] = params
        captured["headers"] = headers
        return _FakeResp()

    monkeypatch.setattr(oa_mod.requests, "get", _fake_get)

    oa_mod.resolve_openalex_pdf("10.1000/test")
    assert captured["params"].get("mailto") == "fetch@test.org"
    assert captured["headers"].get("Authorization") == "Bearer fetch-test-key-abc"


def test_fetch_openalex_without_credentials_is_anonymous(monkeypatch):
    """Without credentials, no mailto/Authorization must be sent."""
    import src.fetch.fetch_openalex as oa_mod
    from src.services.openalex_credentials import OpenAlexCredentials

    monkeypatch.setattr(
        oa_mod, "load_openalex_credentials",
        lambda *a, **k: OpenAlexCredentials(),
    )

    captured = {}

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": []}

    def _fake_get(url, *, params=None, headers=None, timeout=20, proxies=None, **kw):
        captured["params"] = params
        captured["headers"] = headers
        return _FakeResp()

    monkeypatch.setattr(oa_mod.requests, "get", _fake_get)

    oa_mod.resolve_openalex_pdf("10.1000/test")
    assert "mailto" not in captured["params"]
    assert "Authorization" not in captured["headers"]


def test_fetch_openalex_loads_credentials_once(monkeypatch):
    """load_openalex_credentials must be called exactly once per resolve."""
    import src.fetch.fetch_openalex as oa_mod
    from src.services.openalex_credentials import OpenAlexCredentials

    call_count = 0

    def counting_loader(*a, **k):
        nonlocal call_count
        call_count += 1
        return OpenAlexCredentials()

    monkeypatch.setattr(oa_mod, "load_openalex_credentials", counting_loader)

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": []}

    monkeypatch.setattr(oa_mod.requests, "get", lambda *a, **kw: _FakeResp())

    oa_mod.resolve_openalex_pdf("10.1000/test")
    assert call_count == 1, f"Expected 1 call, got {call_count}"


def test_fetch_openalex_does_not_persist_credentials(monkeypatch):
    """FetchResult must not contain credential values in any field."""
    import src.fetch.fetch_openalex as oa_mod
    from src.services.openalex_credentials import OpenAlexCredentials

    monkeypatch.setattr(
        oa_mod, "load_openalex_credentials",
        lambda *a, **k: OpenAlexCredentials(email="no-leak@test.org", api_key="no-leak-key-abc"),
    )

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"open_access": {"is_oa": True, "oa_url": "https://example.org/paper.pdf"}}]}

    monkeypatch.setattr(oa_mod.requests, "get", lambda *a, **kw: _FakeResp())

    result = oa_mod.resolve_openalex_pdf("10.1000/test")
    result_str = str(result)
    assert "no-leak@test.org" not in result_str
    assert "no-leak-key-abc" not in result_str
    assert "no-leak" not in result_str


def test_fetch_openalex_error_does_not_leak_credentials(monkeypatch):
    """Exception messages containing credentials must not leak into log or FetchResult.error."""
    import src.fetch.fetch_openalex as oa_mod
    from src.services.openalex_credentials import OpenAlexCredentials

    monkeypatch.setattr(
        oa_mod, "load_openalex_credentials",
        lambda *a, **k: OpenAlexCredentials(email="err-leak@test.org", api_key="err-leak-key-99999"),
    )

    class LeakyException(Exception):
        def __init__(self):
            super().__init__(
                "GET https://api.openalex.org/works?filter=doi:10.1000/test&mailto=err-leak@test.org"
            )

    monkeypatch.setattr(
        oa_mod.requests, "get",
        lambda *a, **kw: (_ for _ in ()).throw(LeakyException()),
    )

    # Capture logs
    log_lines = []
    monkeypatch.setattr(oa_mod.logger, "warning", lambda msg, *args: log_lines.append(msg.format(*args) if args else msg))

    result = oa_mod.resolve_openalex_pdf("10.1000/test")

    # FetchResult.error must not contain credentials
    assert result.error is not None
    assert "err-leak@test.org" not in result.error
    assert "err-leak-key-99999" not in result.error

    # Logs must not contain credentials
    log_text = "\n".join(log_lines)
    assert "err-leak@test.org" not in log_text, f"Email leaked into log: {log_text}"
    assert "err-leak-key-99999" not in log_text, f"API key leaked into log: {log_text}"
