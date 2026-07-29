"""Contact-email resolution — the single authority, process environment only.

Several scholarly APIs require a real contact address and actively reject
placeholders: Unpaywall answers ``HTTP 422 "Please use your own email address
in API calls."`` for anything under ``example.com``, and Crossref/OpenAlex
demote such callers out of the polite pool.  Sending a placeholder is never a
degraded mode — it is a guaranteed failure that also burns the request budget.

This lives in ``utils`` because both the rate limiter (``src.utils.rate_limit``
mailto override) and the fetch layer need it; keeping two resolvers would give
the same contract two authorities.

No file I/O, no .env, no python-dotenv, no src imports.
"""
from __future__ import annotations

import os
from typing import Mapping


#: Environment variables consulted in order.  The dedicated variable wins;
#: ``MINERU_METADATA_CONTACT_EMAIL`` is the historical rate-limiter name kept
#: for compatibility.
#:
#: Provider credential variables are deliberately absent: those belong to the
#: module that owns them (see ``docs/DEPENDENCIES_AND_EXTERNAL_TOOLS.md`` §9),
#: and duplicating their names here would give one contract two authorities.
#: A caller in the fetch layer that wants to reuse a provider contact address
#: composes the two lookups itself.
CONTACT_EMAIL_ENV_VARS = (
    "MINERU_CONTACT_EMAIL",
    "MINERU_METADATA_CONTACT_EMAIL",
)

#: Domains reserved by RFC 2606 / RFC 6761, plus the placeholder this project
#: used to send.  An address in one of these is not a contact address.
_PLACEHOLDER_DOMAINS = frozenset({
    "example.com",
    "example.org",
    "example.net",
    "example.edu",
    "invalid",
    "localhost",
    "test",
})

CONTACT_EMAIL_MISSING_ERROR = (
    "contact email not configured; set MINERU_CONTACT_EMAIL "
    "(see docs/DEPENDENCIES_AND_EXTERNAL_TOOLS.md)"
)


def is_usable_contact_email(value: str | None) -> bool:
    """Return True when *value* is a real, non-placeholder address."""
    candidate = str(value or "").strip()
    if candidate.count("@") != 1 or any(ch.isspace() for ch in candidate):
        return False
    local, _, domain = candidate.partition("@")
    if not local or not domain or "." not in domain.strip("."):
        return False
    return domain.lower() not in _PLACEHOLDER_DOMAINS


def load_contact_email(env: Mapping[str, str] | None = None) -> str | None:
    """Return the configured contact email, or ``None`` when unusable.

    Priority: process environment variables only, in the order given by
    :data:`CONTACT_EMAIL_ENV_VARS`.  No file-based fallback is supported.

    Pass an explicit ``env`` mapping for testing isolation; when ``env`` is
    ``None`` (the default) the real ``os.environ`` is used.  An empty dict is
    treated as "not configured" — it does **not** fall back to ``os.environ``.

    Placeholder and syntactically unusable addresses return ``None`` so that
    callers fail closed rather than sending a request that cannot succeed.
    """
    source = os.environ if env is None else env
    for name in CONTACT_EMAIL_ENV_VARS:
        candidate = str(source.get(name) or "").strip()
        if is_usable_contact_email(candidate):
            return candidate
    return None


def contact_email_safe_summary(env: Mapping[str, str] | None = None) -> str:
    """Return a one-line summary suitable for logging (never the address)."""
    return f"contact email configured: {'yes' if load_contact_email(env) else 'no'}"
