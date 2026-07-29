"""Host reachability policy for PDF fetching.

Some publishers run bot management (Akamai / Cloudflare) that scores requests
by originating ASN.  When this project runs from a datacenter network, those
hosts answer ``403``/``202`` to *every* request regardless of User-Agent, TLS
fingerprint, cookies, referer, or proxy — the block is on the network, not on
the request.  Retrying them costs a full resolver chain and 30-45s per paper
and can never succeed.

This module is the single place that knows which hosts behave that way, so
the knowledge is data rather than scattered URL substring checks.

Semantics — deliberately advisory, never a hard denial:

- ranking: :func:`is_bot_blocked_host` demotes a candidate URL so that an
  equivalent repository copy is tried first (see ``src/fetch/oa_locations``);
- reporting: a workspace whose only candidates are blocked hosts is reported
  as ``blocked_publisher`` so it can be routed to institutional access;
- skipping: only when the caller explicitly opts in.

The set is empirically derived, not a guess.  Every entry below had a large
number of transport attempts and *zero* successful responses in
``source_records/fetch_result.json``.  Hosts that ever produced a PDF are
deliberately absent even when they sometimes fail.

Reachability is a property of the current egress, so this is a heuristic with
an expiry date: moving to a residential or campus network can make these hosts
work again.  Nothing here blocks a URL the caller explicitly asks for.
"""
from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlsplit


#: Domain suffixes observed to refuse every request from a datacenter egress.
#: Matching is suffix-based, so subdomains are covered (``agupubs`` and
#: ``rmets`` under ``onlinelibrary.wiley.com``, for example).
BOT_BLOCKED_DOMAIN_SUFFIXES = frozenset({
    # Wiley — 441 + 326 + 18 attempts, 0 successes, all HTTP 403.
    "onlinelibrary.wiley.com",
    "api.wiley.com",
    "essopenarchive.org",
    # MDPI — 166 attempts, 0 successes; Akamai edge "Access Denied".
    "mdpi.com",
    # Elsevier — 24 attempts, 0 successes, all HTTP 403.
    "sciencedirect.com",
    # AIP — 20 attempts, 0 successes, all HTTP 403.
    "pubs.aip.org",
    # Taylor & Francis — 12 attempts, 0 successes, all HTTP 403.
    "tandfonline.com",
    # PNAS / AAAS — 8 attempts each, 0 successes, all HTTP 403.
    "pnas.org",
    "sciencemag.org",
    # APS — 8 attempts, 0 successes, all HTTP 403.
    "link.aps.org",
    # Oxford University Press — 8 attempts, 0 successes, all HTTP 403.
    "academic.oup.com",
    # University of California Press — 8 attempts, 0 successes, all HTTP 403.
    "online.ucpress.edu",
})

#: Hosts that are pure DOI redirectors. A failure recorded against these
#: really belongs to whatever publisher they redirected to, so they must never
#: be treated as blocked themselves.
_REDIRECTOR_HOSTS = frozenset({"doi.org", "dx.doi.org"})


def normalize_host(value: str) -> str:
    """Return the lowercase hostname of *value* (URL or bare host).

    Ports, credentials, and a trailing dot are stripped so that
    ``journals.example.org:443`` and ``journals.example.org.`` compare equal.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        host = urlsplit(text if "//" in text else f"//{text}").hostname or ""
    except ValueError:
        return ""
    host = host.lower().rstrip(".")
    # urlsplit is permissive: free text parses into a "hostname". A real host
    # has no whitespace, so reject those rather than reporting garbage.
    return "" if any(ch.isspace() for ch in host) else host


def is_bot_blocked_host(value: str) -> bool:
    """Return True when *value* resolves to a host known to refuse this egress.

    *value* may be a full URL or a bare hostname.  Matching is on registrable
    domain suffix boundaries, so ``agupubs.onlinelibrary.wiley.com`` matches
    ``onlinelibrary.wiley.com`` but ``notwiley.com`` does not match
    ``wiley.com``.
    """
    host = normalize_host(value)
    if not host or host in _REDIRECTOR_HOSTS:
        return False
    return any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in BOT_BLOCKED_DOMAIN_SUFFIXES
    )


def classify_failure(item: Mapping[str, Any]) -> str:
    """Classify a failed fetch report item for operator routing.

    Returns ``"blocked_publisher"`` when the run reached a host that refuses
    this egress and nothing else produced a PDF — those papers need
    institutional access or a different network, not another retry — and
    ``"unresolved"`` otherwise.

    Classification is deliberately post-hoc rather than a pre-filter: whether
    a paper has a reachable repository copy is only known after the OA
    lookups run, so skipping by publisher up front would discard exactly the
    papers those lookups can rescue.
    """
    if str(item.get("status") or "") != "failed":
        return ""
    urls: list[str] = []
    for attempt in item.get("transport_attempts") or []:
        if isinstance(attempt, Mapping):
            urls.append(str(attempt.get("final_url") or ""))
            urls.append(str(attempt.get("request_url") or ""))
    for attempt in item.get("attempts") or []:
        if isinstance(attempt, Mapping):
            urls.append(str(attempt.get("final_url") or ""))
            urls.append(str(attempt.get("candidate_url") or ""))
    return "blocked_publisher" if blocked_hosts_in(urls) else "unresolved"


def blocked_hosts_in(urls: object) -> list[str]:
    """Return the sorted distinct blocked hosts among *urls*.

    Accepts any iterable of strings; non-string items are ignored so callers
    can pass raw provider payloads without pre-filtering.
    """
    if isinstance(urls, str) or not hasattr(urls, "__iter__"):
        urls = [urls]
    found = {
        normalize_host(url)
        for url in urls
        if isinstance(url, str) and is_bot_blocked_host(url)
    }
    return sorted(found)
