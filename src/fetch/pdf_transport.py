"""Direct-first transport for PDF-content fetches.

This module is intentionally scoped to PDF/HTML content retrieval. Metadata
and discovery APIs keep their existing proxy/configuration behavior.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass, replace
import math
import os
import re
import time
import warnings
from types import TracebackType
from typing import Any, Literal, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from src.fetch.host_policy import is_bot_blocked_host


ExpectedContent = Literal["pdf", "html", "any"]
TransportMode = Literal["direct", "proxy"]
UrlQueryPolicy = Literal["strip_all", "allowlist"]

TRANSPORT_POLICY = "direct_then_proxy"
DEFAULT_PDF_PROXY_URL = "http://127.0.0.1:7890"
DEFAULT_DIRECT_TIMEOUT = 30.0
DEFAULT_PROXY_TIMEOUT = 45.0

#: Reason recorded when the proxy retry is skipped as provably futile.
PROXY_SKIP_BOT_BLOCKED = "proxy_skipped_bot_blocked_host"

_SENSITIVE_QUERY_KEYS = {
    "auth",
    "token",
    "key",
    "api_key",
    "apikey",
    "access_token",
    "signature",
    "sig",
    "se",
    "sp",
    "sv",
    "policy",
    "expires",
    "googleaccessid",
    "key-pair-id",
    "x-amz-credential",
    "x-amz-signature",
    "x-amz-security-token",
    "x-amz-expires",
    "x-amz-date",
}
_SENSITIVE_QUERY_PREFIXES = ("x-amz-",)
_URL_FIELD_NAMES = {
    "url",
    "request_url",
    "final_url",
    "pdf_url",
    "landing_url",
    "source_url",
    "redirect_url",
    "redirect_history",
    "redirect_chain",
}


@dataclass(frozen=True)
class PdfTransportConfig:
    proxy_url: str = DEFAULT_PDF_PROXY_URL
    proxy_fallback_enabled: bool = True
    direct_timeout: float = DEFAULT_DIRECT_TIMEOUT
    proxy_timeout: float = DEFAULT_PROXY_TIMEOUT


@dataclass(frozen=True)
class ContentInspection:
    accepted: bool
    detected_content: Literal["pdf", "html", "unknown"]
    reason_code: str | None
    content_type: str
    prefix: bytes


@dataclass(frozen=True)
class TransportAttempt:
    request_url: str
    mode: TransportMode
    success: bool
    status_code: int | None = None
    elapsed_seconds: float = 0.0
    final_url: str = ""
    content_type: str = ""
    error_type: str = ""
    error_message: str = ""
    proxy_configured: bool = False
    detected_content: str = "unknown"
    reason_code: str = ""

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "request_url": sanitize_url_for_persistence(self.request_url),
            "mode": self.mode,
            "success": self.success,
            "status_code": self.status_code,
            "elapsed_seconds": round(float(self.elapsed_seconds), 3),
            "final_url": sanitize_url_for_persistence(self.final_url),
            "content_type": self.content_type,
            "error_type": self.error_type,
            "error_message": _redact_error_message(self.error_message),
            "proxy_configured": self.proxy_configured,
            "detected_content": self.detected_content,
            "reason_code": self.reason_code,
        }


@dataclass
class TransportResult:
    response: requests.Response | None
    attempts: list[TransportAttempt] = field(default_factory=list)
    error: str = ""
    _session: requests.Session | None = None

    def __enter__(self) -> "TransportResult":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.response is not None:
            _close_response(self.response)
        if self._session is not None:
            self._session.close()

    @property
    def safe_attempts(self) -> list[dict[str, Any]]:
        return [attempt.to_safe_dict() for attempt in self.attempts]


def load_pdf_transport_config(env: Mapping[str, str] | None = None) -> PdfTransportConfig:
    """Load PDF-content transport configuration.

    ``env={}`` is a real empty source and must not fall back to the process
    environment; tests rely on that injectability.
    """
    source = os.environ if env is None else env
    return PdfTransportConfig(
        proxy_url=str(source.get("MINERU_PDF_PROXY_URL") or DEFAULT_PDF_PROXY_URL),
        proxy_fallback_enabled=not _truthy(source.get("MINERU_PDF_DISABLE_PROXY_FALLBACK")),
        direct_timeout=_float_env(source.get("MINERU_PDF_DIRECT_TIMEOUT"), DEFAULT_DIRECT_TIMEOUT),
        proxy_timeout=_float_env(source.get("MINERU_PDF_PROXY_TIMEOUT"), DEFAULT_PROXY_TIMEOUT),
    )


def fetch_url_direct_then_proxy(
    url: str,
    *,
    expected_content: ExpectedContent,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    stream: bool = False,
    allow_redirects: bool = True,
    direct_timeout: float | tuple[float, float] | None = None,
    proxy_timeout: float | tuple[float, float] | None = None,
    config: PdfTransportConfig | None = None,
    method: str = "GET",
    data: Any = None,
    json: Any = None,
) -> TransportResult:
    """Fetch *url* directly first, then via explicit proxy when retryable.

    Successful HTTP responses are accepted only when their body matches
    ``expected_content``. A direct content mismatch is proxy-retryable.
    """
    cfg = config or load_pdf_transport_config()
    direct_timeout = direct_timeout if direct_timeout is not None else cfg.direct_timeout
    proxy_timeout = proxy_timeout if proxy_timeout is not None else cfg.proxy_timeout
    attempts: list[TransportAttempt] = []

    invalid_error = _invalid_url_error(url)
    if invalid_error:
        attempts.append(
            TransportAttempt(
                request_url=url,
                mode="direct",
                success=False,
                error_type=invalid_error,
                error_message=invalid_error,
            )
        )
        return TransportResult(response=None, attempts=attempts, error=invalid_error)

    direct = _request_once(
        url,
        mode="direct",
        timeout=direct_timeout,
        proxy_url="",
        headers=headers,
        params=params,
        stream=stream,
        allow_redirects=allow_redirects,
        method=method,
        data=data,
        json=json,
    )
    attempts.append(direct.attempt)

    direct = _inspect_outcome(direct, expected_content)
    attempts[-1] = direct.attempt

    if (
        direct.response is not None
        and not _should_fallback_for_status(direct.response.status_code)
        and (direct.response.status_code >= 400 or direct.attempt.success)
    ):
        return TransportResult(response=direct.response, attempts=attempts, _session=direct.session)

    if direct.response is None and not direct.retryable:
        if direct.session is not None:
            direct.session.close()
        return TransportResult(response=None, attempts=attempts, error=direct.attempt.error_message)

    status_code = direct.attempt.status_code
    final_url = direct.attempt.final_url or url
    if direct.response is not None:
        _close_response(direct.response)
    if direct.session is not None:
        direct.session.close()

    if not cfg.proxy_fallback_enabled or not cfg.proxy_url:
        return TransportResult(response=None, attempts=attempts, error=direct.attempt.error_message)

    # A 403 from a host that runs ASN-scoped bot management is a verdict on
    # the request's network identity, not on the request itself. Replaying it
    # through a proxy only produces the same 403 at double the wall-clock
    # cost -- measured: 551 direct 403s produced 548 proxy 403s and zero
    # successes. Connection-level failures are NOT skipped, because there the
    # proxy does convert failures into successes.
    if status_code == 403 and is_bot_blocked_host(final_url):
        return TransportResult(
            response=None,
            attempts=attempts,
            error=PROXY_SKIP_BOT_BLOCKED,
        )

    proxy = _request_once(
        url,
        mode="proxy",
        timeout=proxy_timeout,
        proxy_url=cfg.proxy_url,
        headers=headers,
        params=params,
        stream=stream,
        allow_redirects=allow_redirects,
        method=method,
        data=data,
        json=json,
    )
    attempts.append(proxy.attempt)
    proxy = _inspect_outcome(proxy, expected_content)
    attempts[-1] = proxy.attempt
    if proxy.response is not None and not proxy.attempt.success and proxy.response.status_code < 400:
        _close_response(proxy.response)
        if proxy.session is not None:
            proxy.session.close()
        return TransportResult(
            response=None,
            attempts=attempts,
            error=proxy.attempt.reason_code or "content_mismatch",
        )
    return TransportResult(
        response=proxy.response,
        attempts=attempts,
        error=proxy.attempt.error_message if proxy.response is None else "",
        _session=proxy.session,
    )


def sanitize_transport_url(url: str) -> str:
    """Return a diagnostic-safe URL, preserving only non-sensitive query keys."""
    return _sanitize_url(url, query_policy="allowlist")


def sanitize_url_for_persistence(
    url: str,
    *,
    query_policy: UrlQueryPolicy = "strip_all",
    allowed_query_params: set[str] | None = None,
) -> str:
    """Return a URL safe for logs, reports, sidecars, and metadata.

    The default is intentionally conservative: drop the entire query string
    because signed download URLs often use provider-specific parameters.
    """
    return _sanitize_url(
        url,
        query_policy=query_policy,
        allowed_query_params=allowed_query_params,
    )


def sanitize_for_persistence(value: Any, *, parent_key: str = "") -> Any:
    """Recursively redact URLs, including URLs embedded in free text."""
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, BaseException):
        value = str(value)
    if isinstance(value, dict):
        return {
            key: sanitize_for_persistence(item, parent_key=str(key))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        if _is_url_field(parent_key):
            return [
                sanitize_url_for_persistence(item) if isinstance(item, str) else sanitize_for_persistence(item)
                for item in value
            ]
        return [sanitize_for_persistence(item) for item in value]
    if isinstance(value, str):
        if _is_url_field(parent_key):
            return sanitize_url_for_persistence(value)
        return _redact_urls_in_text(value)
    return value


_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def _redact_urls_in_text(value: str) -> str:
    def replace_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        core = raw.rstrip(".,;:!?)]]}")
        suffix = raw[len(core):]
        return sanitize_url_for_persistence(core) + suffix
    return _URL_IN_TEXT_RE.sub(replace_url, value)


def _sanitize_url(
    url: str,
    *,
    query_policy: UrlQueryPolicy,
    allowed_query_params: set[str] | None = None,
) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except Exception:
        return ""
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return ""
    try:
        port = parts.port
    except ValueError:
        return ""
    netloc = parts.netloc.rsplit("@", 1)[-1]
    if port is not None:
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = f"{host}:{port}"
    elif "@" in parts.netloc:
        netloc = parts.netloc.rsplit("@", 1)[-1]
    if query_policy == "strip_all":
        query = ""
    else:
        allowed = {k.lower() for k in allowed_query_params or set()}
        query = urlencode(
            [
                (k, v)
                for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if (not allowed or k.lower() in allowed) and not _is_sensitive_query_key(k)
            ],
            doseq=True,
        )
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def _is_sensitive_query_key(key: str) -> bool:
    lowered = str(key or "").lower()
    return lowered in _SENSITIVE_QUERY_KEYS or any(
        lowered.startswith(prefix) for prefix in _SENSITIVE_QUERY_PREFIXES
    )


def _is_url_field(key: str) -> bool:
    lowered = str(key or "").lower()
    return lowered in _URL_FIELD_NAMES or lowered.endswith("_url") or lowered.endswith("_urls")


def inspect_response_content(
    *, response: requests.Response, expected_content: ExpectedContent
) -> ContentInspection:
    """Classify a response from headers and a bounded body prefix."""
    content_type = str(response.headers.get("content-type", "")).split(";", 1)[0].strip().lower()
    prefix = _response_prefix(response)
    stripped = prefix.lstrip().lower()
    is_pdf = prefix.startswith(b"%PDF-")
    html_marker = stripped.startswith((b"<!doctype html", b"<html", b"<?xml")) or any(
        marker in stripped
        for marker in (b"captcha", b"cloudflare", b"access denied", b"sign in", b"login")
    )
    is_html = content_type in {"text/html", "application/xhtml+xml"} or html_marker
    detected: Literal["pdf", "html", "unknown"] = "pdf" if is_pdf else ("html" if is_html else "unknown")
    if not prefix:
        return ContentInspection(False, detected, "empty_body", content_type, prefix)
    if expected_content == "any":
        return ContentInspection(True, detected, None, content_type, prefix)
    if expected_content == "pdf":
        if is_html:
            reason = "challenge_or_html" if html_marker else "expected_pdf_received_html"
            return ContentInspection(False, detected, reason, content_type, prefix)
        if not is_pdf:
            return ContentInspection(False, detected, "missing_pdf_magic", content_type, prefix)
        return ContentInspection(True, detected, None, content_type, prefix)
    if is_pdf:
        return ContentInspection(False, detected, "expected_html_received_pdf", content_type, prefix)
    if not is_html:
        return ContentInspection(False, detected, "expected_html_received_unknown", content_type, prefix)
    return ContentInspection(True, detected, None, content_type, prefix)


def _response_prefix(response: requests.Response, limit: int = 512) -> bytes:
    cached = getattr(response, "_content", False)
    if isinstance(cached, (bytes, bytearray)):
        return bytes(cached[:limit])
    raw = getattr(response, "raw", None)
    if raw is not None and hasattr(raw, "read"):
        try:
            prefix = raw.read(limit, decode_content=True)
        except TypeError:
            prefix = raw.read(limit)
        prefix = bytes(prefix or b"")
        setattr(response, "_mineru_prefetched_prefix", prefix)
        return prefix
    content = getattr(response, "content", b"")
    return bytes(content[:limit]) if isinstance(content, (bytes, bytearray)) else b""


@dataclass
class _RequestOutcome:
    response: requests.Response | None
    session: requests.Session | None
    attempt: TransportAttempt
    retryable: bool


def _inspect_outcome(outcome: _RequestOutcome, expected_content: ExpectedContent) -> _RequestOutcome:
    response = outcome.response
    if response is None or response.status_code >= 400:
        return outcome
    inspection = inspect_response_content(response=response, expected_content=expected_content)
    attempt = replace(
        outcome.attempt,
        success=inspection.accepted,
        detected_content=inspection.detected_content,
        reason_code=inspection.reason_code or "",
        error_type="" if inspection.accepted else "ContentMismatch",
        error_message="" if inspection.accepted else (inspection.reason_code or "content_mismatch"),
    )
    return _RequestOutcome(response, outcome.session, attempt, not inspection.accepted)


def _request_once(
    url: str,
    *,
    mode: TransportMode,
    timeout: float | tuple[float, float],
    proxy_url: str,
    headers: Mapping[str, str] | None,
    params: Mapping[str, Any] | None,
    stream: bool,
    allow_redirects: bool,
    method: str,
    data: Any,
    json: Any,
) -> _RequestOutcome:
    session = requests.Session()
    session.trust_env = False
    start = time.monotonic()
    proxies = {"http": proxy_url, "https": proxy_url} if mode == "proxy" and proxy_url else None
    try:
        response = session.request(
            method,
            url,
            headers=dict(headers or {}),
            params=dict(params or {}),
            data=data,
            json=json,
            timeout=timeout,
            allow_redirects=allow_redirects,
            stream=stream,
            proxies=proxies,
        )
    except Exception as exc:
        elapsed = time.monotonic() - start
        retryable = mode == "direct" and _is_retryable_direct_exception(exc)
        terminal_invalid = isinstance(
            exc,
            (
                requests.exceptions.MissingSchema,
                requests.exceptions.InvalidSchema,
                requests.exceptions.InvalidURL,
                requests.exceptions.InvalidProxyURL,
            ),
        )
        if terminal_invalid:
            retryable = False
        session.close()
        attempt = TransportAttempt(
            request_url=url,
            mode=mode,
            success=False,
            elapsed_seconds=elapsed,
            error_type=type(exc).__name__,
            error_message=type(exc).__name__,
            proxy_configured=bool(proxy_url) if mode == "proxy" else False,
        )
        return _RequestOutcome(None, None, attempt, retryable)

    elapsed = time.monotonic() - start
    status_code = response.status_code if response.status_code else None
    attempt = TransportAttempt(
        request_url=url,
        mode=mode,
        success=status_code is not None and 200 <= status_code < 400,
        status_code=status_code,
        elapsed_seconds=elapsed,
        final_url=response.url or url,
        content_type=response.headers.get("content-type", ""),
        error_type="" if status_code is not None and status_code < 400 else "HTTPStatus",
        error_message="" if status_code is not None and status_code < 400 else f"HTTP {status_code}",
        proxy_configured=bool(proxy_url) if mode == "proxy" else False,
    )
    retryable = mode == "direct" and _should_fallback_for_status(status_code)
    return _RequestOutcome(response, session, attempt, retryable)


def _invalid_url_error(url: str) -> str:
    try:
        parts = urlsplit(url)
        parts.port
    except Exception:
        return "InvalidURL"
    if not parts.scheme:
        return "MissingSchema"
    if parts.scheme.lower() not in {"http", "https"}:
        return "InvalidSchema"
    if not parts.netloc:
        return "InvalidURL"
    return ""


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _should_fallback_for_status(status_code: int | None) -> bool:
    if status_code is None:
        return False
    if status_code in {403, 408, 429}:
        return True
    if 500 <= status_code <= 599:
        return True
    return False


def _is_retryable_direct_exception(exc: BaseException) -> bool:
    if isinstance(exc, (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout)):
        return True
    if isinstance(exc, requests.exceptions.Timeout):
        return True
    if isinstance(
        exc,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.SSLError,
            requests.exceptions.TooManyRedirects,
        ),
    ):
        return True
    return False


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _float_env(value: str | None, default: float) -> float:
    try:
        if value in (None, ""):
            return default
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed) or parsed <= 0:
        warnings.warn("invalid MINERU_PDF timeout value; using default", RuntimeWarning)
        return default
    return parsed


def _redact_error_message(message: str) -> str:
    if not message:
        return ""
    # Avoid preserving credential-bearing URLs or exception detail. Error
    # classes/statuses are enough for diagnostics; callers still have safe URL,
    # status, final URL, and content type fields.
    if "http://" in message or "https://" in message:
        return "redacted"
    return message[:200]
