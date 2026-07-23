"""Typed provider and state errors for DOI discovery.

This module is the single source of truth for provider request failure
classification.  Business modules must never branch on raw exception
strings or generic ``Exception``; they branch on these types.

Classification contract (frozen in ``docs/PROJECT_CONTRACT.md``):

- 429                              -> :class:`ProviderRateLimited`
- 408, 425, 500, 502, 503, 504     -> :class:`ProviderTransientError`
- timeout                          -> :class:`ProviderTimeoutError`
- SSL reset / connection reset     -> :class:`ProviderConnectionError`
- 400, 404, 410, 422               -> :class:`ProviderPermanentError`
- 401, 403 (no rate-limit signal)  -> :class:`ProviderAuthError`
- malformed JSON / schema          -> :class:`ProviderProtocolError`
- provider reports no next cursor  -> :class:`ProviderExhausted` (signal, not failure)
- journal / state write failure    -> :class:`LocalPersistenceError`
- cursor CAS conflict              -> :class:`StateConflictError`

Only rate-limited, transient, timeout and connection errors are retryable.
Exhaustion is a *signal* and must never be treated as a failure; transient
failures must never be treated as exhaustion.
"""
from __future__ import annotations

from typing import Any


class ProviderError(Exception):
    """Base class for all provider request failures."""

    #: Whether the failing request is worth retrying (with backoff).
    retryable: bool = False

    def __init__(self, message: str = "", *, provider: str = "", purpose: str = "") -> None:
        super().__init__(message)
        self.provider = provider
        self.purpose = purpose


class ProviderRateLimited(ProviderError):
    """HTTP 429 (or 403 with Retry-After evidence).  Honor Retry-After."""

    retryable = True

    def __init__(
        self,
        message: str = "provider rate limited",
        *,
        retry_after_seconds: float | None = None,
        http_status: int | None = 429,
        **kw: Any,
    ) -> None:
        super().__init__(message, **kw)
        self.retry_after_seconds = retry_after_seconds
        self.http_status = http_status


class ProviderTransientError(ProviderError):
    """Retryable 5xx / 408 / 425 server-side failure."""

    retryable = True

    def __init__(self, message: str = "transient provider error", *, http_status: int | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.http_status = http_status


class ProviderTimeoutError(ProviderTransientError):
    """Request timeout (connect/read)."""


class ProviderCancelledError(ProviderError):
    """A caller cancelled while waiting at the shared provider gate."""


class ProviderConnectionError(ProviderTransientError):
    """Connection reset / SSL failure / DNS failure."""


class ProviderProtocolError(ProviderError):
    """Response body cannot be interpreted (malformed JSON, schema drift).

    Not retried by default: re-issuing the identical request rarely fixes a
    protocol mismatch, and the failure is recorded for operator attention.
    """

    retryable = False


class ProviderAuthError(ProviderError):
    """401 / 403 without rate-limit evidence.  Never retried."""

    retryable = False

    def __init__(self, message: str = "provider authentication failure", *, http_status: int | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.http_status = http_status


class ProviderPermanentError(ProviderError):
    """400 / 404 / 410 / 422 and other non-retryable request failures."""

    retryable = False

    def __init__(self, message: str = "permanent provider request error", *, http_status: int | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.http_status = http_status


class ProviderExhausted(ProviderError):
    """Internal signal: provider reports no next cursor for this lane.

    This is NOT a failure and is never raised across the network boundary;
    it exists so the state machine can express the exhaustion event with a
    typed object.  ``retryable`` stays False because there is nothing to
    retry — the lane is done.
    """


class ProviderRequestBudgetExhausted(ProviderError):
    """Batch-level provider request budget reached (clean stop signal)."""


class CircuitOpenError(ProviderError):
    """The provider circuit breaker is open; requests are short-circuited."""


class LocalPersistenceError(Exception):
    """Journal / notebook / state write failed locally."""


class StateConflictError(Exception):
    """Cursor CAS conflict or unresolvable persisted-state ambiguity."""


# ── Classification ───────────────────────────────────────────────────

_TERMINAL_STATUSES = frozenset({400, 404, 410, 422})
_AUTH_STATUSES = frozenset({401})
_TRANSIENT_STATUSES = frozenset({408, 425, 500, 502, 503, 504})


def classify_response_failure(
    status: int,
    *,
    retry_after_seconds: float | None = None,
    provider: str = "",
    purpose: str = "",
) -> ProviderError:
    """Map an HTTP failure status onto a typed :class:`ProviderError`."""
    kw = {"provider": provider, "purpose": purpose}
    if status == 429:
        return ProviderRateLimited(retry_after_seconds=retry_after_seconds, http_status=status, **kw)
    if status == 403:
        if retry_after_seconds is not None:
            return ProviderRateLimited(retry_after_seconds=retry_after_seconds, http_status=status, **kw)
        return ProviderAuthError(http_status=status, **kw)
    if status in _AUTH_STATUSES:
        return ProviderAuthError(http_status=status, **kw)
    if status in _TERMINAL_STATUSES:
        return ProviderPermanentError(http_status=status, **kw)
    if status in _TRANSIENT_STATUSES:
        return ProviderTransientError(http_status=status, **kw)
    # Unknown status: safe default is transient (retryable) for 5xx-like,
    # permanent for other 4xx-like.
    if status >= 500:
        return ProviderTransientError(http_status=status, **kw)
    return ProviderPermanentError(http_status=status, **kw)


def classify_transport_error(
    exc: BaseException,
    *,
    provider: str = "",
    purpose: str = "",
) -> ProviderError:
    """Map a transport-level exception (no HTTP response) onto a typed error.

    Uses duck-typing on exception class names so this module never imports
    ``requests`` (keeping it importable in offline/test contexts).
    """
    name = type(exc).__name__
    kw = {"provider": provider, "purpose": purpose}
    if "Timeout" in name:
        return ProviderTimeoutError(str(exc) or "request timeout", **kw)
    if "SSLError" in name or "SSL" in name:
        return ProviderConnectionError(str(exc) or "SSL failure", **kw)
    if "ConnectionError" in name or "Connection" in name:
        return ProviderConnectionError(str(exc) or "connection failure", **kw)
    # Unknown transport failure: treat as connection-class transient.
    return ProviderConnectionError(str(exc) or f"transport failure ({name})", **kw)
