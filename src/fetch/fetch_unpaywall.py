"""Unpaywall OA PDF lookup.

Unpaywall reports every place a paper can be downloaded from.  This module
keeps *all* of them (see ``src/fetch/oa_locations``) instead of collapsing the
list to ``best_oa_location``, because Unpaywall's "best" is the publisher copy
and the publisher is exactly the host that refuses a datacenter egress.
"""
from urllib.parse import quote

from loguru import logger

from src.discovery.providers.provider_client import ProviderRuntime, RequestSpec
from src.fetch.models import FetchResult
from src.fetch.oa_locations import candidates_from_unpaywall
from src.fetch.openalex_credentials import load_openalex_credentials
from src.utils.contact_email import (
    CONTACT_EMAIL_MISSING_ERROR,
    is_usable_contact_email,
    load_contact_email,
)


UNPAYWALL_URL = "https://api.unpaywall.org/v2"
UNPAYWALL_PROVIDER = "unpaywall"


def _contact_email() -> str | None:
    """Return a usable contact address for Unpaywall.

    Falls back to the configured provider contact address, which is by
    definition a working one; that name stays owned by
    ``openalex_credentials`` rather than being duplicated into ``utils``.
    """
    email = load_contact_email()
    if email:
        return email
    fallback = load_openalex_credentials().email
    return fallback if is_usable_contact_email(fallback) else None


def resolve_unpaywall(doi: str) -> FetchResult:
    # Unpaywall rejects placeholder addresses with HTTP 422 ("Please use your
    # own email address in API calls."), so an unset contact email is a hard
    # skip -- sending one anyway cannot succeed and only burns the budget.
    email = _contact_email()
    if not email:
        logger.warning("Unpaywall lane skipped: {}", CONTACT_EMAIL_MISSING_ERROR)
        return FetchResult(doi=doi, source="unpaywall", error=CONTACT_EMAIL_MISSING_ERROR)

    candidate_url = f"{UNPAYWALL_URL}/{quote(doi, safe='')}"
    spec = RequestSpec(
        provider=UNPAYWALL_PROVIDER,
        purpose="metadata_resolution",
        url=candidate_url,
        params={"email": email},
        timeout_seconds=20,
    )
    try:
        data = ProviderRuntime.get().client(UNPAYWALL_PROVIDER).execute(spec).json()
    except Exception as exc:
        logger.warning("Unpaywall lookup failed for {!r}: {}", doi, type(exc).__name__)
        return FetchResult(doi=doi, source="unpaywall", error=str(exc))

    if not data.get("is_oa"):
        return FetchResult(doi=doi, source="unpaywall", oa_status="closed", metadata=data, error="not OA")

    candidates = candidates_from_unpaywall(data)
    if not candidates:
        return FetchResult(
            doi=doi, source="unpaywall", oa_status=data.get("oa_status") or "oa",
            metadata=data, error="no OA PDF URL",
        )

    best = candidates[0]
    meta = dict(data)
    if not best.is_direct_pdf:
        meta["maybe_landing_page"] = True
    return FetchResult(
        doi=doi,
        success=True,
        source="unpaywall",
        candidate_url=candidate_url,
        pdf_url=best.url,
        pdf_candidates=[candidate.to_dict() for candidate in candidates],
        oa_status=data.get("oa_status") or "oa",
        license=best.license,
        metadata=meta,
    )
