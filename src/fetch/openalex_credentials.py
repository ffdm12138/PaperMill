"""Centralized OpenAlex credential loading — process environment only.

No file I/O, no .env, no python-dotenv, no project imports.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional


@dataclass(frozen=True, repr=False)
class OpenAlexCredentials:
    """Container for OpenAlex credentials.

    Fields are ``None`` when not configured (not empty string).
    Use ``.email_configured`` / ``.api_key_configured`` to check presence.
    """

    email: str | None = None
    api_key: str | None = None

    @property
    def email_configured(self) -> bool:
        return self.email is not None

    @property
    def api_key_configured(self) -> bool:
        return self.api_key is not None

    def safe_summary(self) -> str:
        """Return a one-line summary suitable for logging (no real values)."""
        return (
            "OpenAlex credentials configured: "
            f"email={'yes' if self.email_configured else 'no'} "
            f"api_key={'yes' if self.api_key_configured else 'no'}"
        )

    def __repr__(self) -> str:
        return self.safe_summary()


def load_openalex_credentials(
    env: Mapping[str, str] | None = None,
) -> OpenAlexCredentials:
    """Load OpenAlex credentials.

    Priority: process environment variables only.
    No file-based fallback is supported.

    Pass an explicit ``env`` mapping for testing isolation; when ``env`` is
    ``None`` (the default) the real ``os.environ`` is used.  An empty dict
    is treated as "no credentials set" — it does **not** fall back to
    ``os.environ``.
    """
    source = os.environ if env is None else env

    email = _clean_optional_value(source.get("OPENALEX_EMAIL"))
    api_key = _clean_optional_value(source.get("OPENALEX_API_KEY"))

    return OpenAlexCredentials(email=email, api_key=api_key)


def _clean_optional_value(value: str | None) -> str | None:
    """Strip whitespace; return ``None`` for empty / all-whitespace."""
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def safe_request_error_summary(exc: Exception) -> str:
    """Return a safe one-line error summary without request details.

    ``str(exc)`` on ``requests`` exceptions may include the full request URL
    (and therefore ``mailto=…`` query parameters).  This function extracts
    only the exception type name and, if available, the HTTP status code.
    """
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    name = type(exc).__name__
    return f"{name} (HTTP {status_code})" if status_code else name
