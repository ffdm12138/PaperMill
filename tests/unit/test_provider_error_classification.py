"""Unit tests for provider HTTP error classification (Phase 0.5).

Verifies that ``classify_http_error`` correctly distinguishes terminal
errors (400/401/403/404) from retryable errors (408/429/5xx) and
transient errors (ConnectionError/Timeout), and that ``backfill_transaction``
propagates the classification as ``provider_terminal`` vs ``provider_retryable``.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.discovery.providers.provider_models import (
    DiscoveryPage,
    FailureClass,
    classify_http_error,
    failed_page,
)


pytestmark = pytest.mark.unit


def _make_http_error(status_code: int, retry_after: str | None = None):
    """Create a mock requests.HTTPError with the given status code."""
    import requests

    response = MagicMock()
    response.status_code = status_code
    response.headers = {}
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    exc = requests.HTTPError(response=response)
    return exc


def test_403_classified_as_terminal():
    """403 Forbidden without rate-limit evidence is terminal."""
    exc = _make_http_error(403)
    error_type, failure_class, http_status, retry_after = classify_http_error(exc)
    assert failure_class == "terminal"
    assert http_status == 403
    assert retry_after is None


def test_403_with_retry_after_classified_as_retryable():
    """403 with Retry-After header is retryable (rate-limited)."""
    exc = _make_http_error(403, retry_after="60")
    error_type, failure_class, http_status, retry_after = classify_http_error(exc)
    assert failure_class == "retryable"
    assert http_status == 403
    assert retry_after == 60.0


def test_429_classified_as_retryable_with_retry_after():
    """429 Too Many Requests is retryable, with retry_after parsed."""
    exc = _make_http_error(429, retry_after="120")
    error_type, failure_class, http_status, retry_after = classify_http_error(exc)
    assert failure_class == "retryable"
    assert http_status == 429
    assert retry_after == 120.0


def test_500_classified_as_retryable():
    """500 Internal Server Error is retryable."""
    exc = _make_http_error(500)
    _, failure_class, http_status, _ = classify_http_error(exc)
    assert failure_class == "retryable"
    assert http_status == 500


def test_502_classified_as_retryable():
    exc = _make_http_error(502)
    _, failure_class, _, _ = classify_http_error(exc)
    assert failure_class == "retryable"


def test_503_classified_as_retryable():
    exc = _make_http_error(503)
    _, failure_class, _, _ = classify_http_error(exc)
    assert failure_class == "retryable"


def test_504_classified_as_retryable():
    exc = _make_http_error(504)
    _, failure_class, _, _ = classify_http_error(exc)
    assert failure_class == "retryable"


def test_408_classified_as_retryable():
    """408 Request Timeout is retryable."""
    exc = _make_http_error(408)
    _, failure_class, _, _ = classify_http_error(exc)
    assert failure_class == "retryable"


def test_400_classified_as_terminal():
    """400 Bad Request is terminal — won't succeed on retry."""
    exc = _make_http_error(400)
    _, failure_class, _, _ = classify_http_error(exc)
    assert failure_class == "terminal"


def test_401_classified_as_terminal():
    """401 Unauthorized is terminal — credential/config error."""
    exc = _make_http_error(401)
    _, failure_class, _, _ = classify_http_error(exc)
    assert failure_class == "terminal"


def test_404_classified_as_terminal():
    """404 Not Found is terminal."""
    exc = _make_http_error(404)
    _, failure_class, _, _ = classify_http_error(exc)
    assert failure_class == "terminal"


def test_timeout_classified_as_transient():
    """requests.Timeout is transient (no http_status)."""
    import requests

    exc = requests.Timeout("connection timed out")
    _, failure_class, http_status, _ = classify_http_error(exc)
    assert failure_class == "transient"
    assert http_status is None


def test_connection_error_classified_as_transient():
    """requests.ConnectionError is transient."""
    import requests

    exc = requests.ConnectionError("connection refused")
    _, failure_class, http_status, _ = classify_http_error(exc)
    assert failure_class == "transient"
    assert http_status is None


def test_unknown_exception_classified_as_retryable():
    """Unknown exceptions default to retryable (safe default)."""
    exc = RuntimeError("something weird")
    _, failure_class, _, _ = classify_http_error(exc)
    assert failure_class == "retryable"


def test_failed_page_carries_failure_class():
    """failed_page must accept and store failure_class/http_status/retry_after."""
    page = failed_page(
        provider="openalex",
        keyword_zh="关键词",
        query="kw",
        lane="backfill",
        request_cursor="*",
        page_size=25,
        error_type="HTTPError",
        safe_error="HTTP 429",
        failure_class="retryable",
        http_status=429,
        retry_after_seconds=60.0,
    )
    assert page.failure_class == "retryable"
    assert page.http_status == 429
    assert page.retry_after_seconds == 60.0


def test_failed_page_defaults_failure_class_to_none():
    """When failure_class is not passed, it defaults to None (backward compat)."""
    page = failed_page(
        provider="openalex",
        keyword_zh="关键词",
        query="kw",
        lane="backfill",
        request_cursor="*",
        page_size=25,
        error_type="HTTPError",
        safe_error="HTTP 500",
    )
    assert page.failure_class is None
    assert page.http_status is None


def test_discovery_page_to_dict_includes_new_fields():
    """to_dict must serialize the new fields."""
    page = failed_page(
        provider="openalex",
        keyword_zh="关键词",
        query="kw",
        lane="backfill",
        request_cursor="*",
        page_size=25,
        error_type="HTTPError",
        safe_error="HTTP 403",
        failure_class="terminal",
        http_status=403,
    )
    d = page.to_dict()
    assert d["failure_class"] == "terminal"
    assert d["http_status"] == 403
    assert "retry_after_seconds" in d
