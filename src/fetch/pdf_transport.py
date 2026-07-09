"""Direct-first transport for PDF-content fetches.

This module is intentionally scoped to PDF/HTML content retrieval. Metadata
and discovery APIs keep their existing proxy/configuration behavior.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
import time
import warnings
from types import TracebackType
from typing import Any, Literal, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests


ExpectedContent = Literal["pdf", "html", "any"]
TransportMode = Literal["direct", "proxy"]
UrlQueryPolicy = Literal["strip_all", "allowlist"]

TRANSPORT_POLICY = "direct_then_proxy"
DEFAULT_PDF_PROXY_URL = "http://127.0.0.1:7890"
DEFAULT_DIRECT_TIMEOUT = 30.0
DEFAULT_PROXY_TIMEOUT = 45.0

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
            close_response = getattr(self.response, "close", None)
            if callable(close_response):
                close_response()
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
    timeout: float | tuple[float, float] | None = None,
    config: PdfTransportConfig | None = None,
    method: str = "GET",
    data: Any = None,
    json: Any = None,
) -> TransportResult:
    """Fetch *url* directly first, then via explicit proxy when retryable.

    The transport layer does not validate PDF/HTML bodies and does not call
    ``raise_for_status()``. Callers inspect terminal responses and perform
    content validation inside the returned context manager.
    """
    del expected_content  # content validation belongs to callers
    cfg = config or load_pdf_transport_config()
    direct_timeout = timeout if timeout is not None else cfg.direct_timeout
    proxy_timeout = timeout if timeout is not None else cfg.proxy_timeout
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

    if direct.response is not None and not _should_fallback_for_status(direct.response.status_code):
        return TransportResult(response=direct.response, attempts=attempts, _session=direct.session)

    if direct.response is None and not direct.retryable:
        if direct.session is not None:
            direct.session.close()
        return TransportResult(response=None, attempts=attempts, error=direct.attempt.error_message)

    if direct.response is not None:
        direct.response.close()
    if direct.session is not None:
        direct.session.close()

    if not cfg.proxy_fallback_enabled or not cfg.proxy_url:
        return TransportResult(response=None, attempts=attempts, error=direct.attempt.error_message)

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


def sanitize_url_fields(value: Any, *, parent_key: str = "") -> Any:
    """Sanitize only values whose field names have URL semantics."""
    if isinstance(value, dict):
        return {
            key: sanitize_url_fields(item, parent_key=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        if _is_url_field(parent_key):
            return [
                sanitize_url_for_persistence(item) if isinstance(item, str) else sanitize_url_fields(item)
                for item in value
            ]
        return [sanitize_url_fields(item) for item in value]
    if isinstance(value, str) and _is_url_field(parent_key):
        return sanitize_url_for_persistence(value)
    return value


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


@dataclass
class _RequestOutcome:
    response: requests.Response | None
    session: requests.Session | None
    attempt: TransportAttempt
    retryable: bool


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
