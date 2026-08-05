from __future__ import annotations

import argparse

import pytest

import scripts.fetch_pdf_for_paper_raw as fetch_cli
from src.fetch.access_policy import AccessMode, AccessPolicy
from src.fetch.resolver_registry import RESOLVER_REGISTRY


pytestmark = pytest.mark.contract


def _args(resolver: str, *, base_url: str = "", url_template: str = "") -> argparse.Namespace:
    return argparse.Namespace(
        resolver=resolver,
        base_url=base_url,
        url_template=url_template,
        timeout=30,
    )


def test_auto_fetch_policy_order():
    policy = fetch_cli._build_policy(_args("auto"), {})

    # semantic_scholar is intentionally absent from the auto chain
    # (2026-08-01): the API is unreachable from this egress
    # (ProviderPermanentError on every lookup) and each paper still pays
    # ~9 serialized 3s-paced lookups/retries for it, capping the whole
    # batch at ~2 papers/min.  `--resolver oa` keeps the full OA list.
    assert policy.enabled_resolver_names() == [
        "original_link",
        "unpaywall",
        "openalex",
        "arxiv",
        "publisher_oa",
        "springer_direct",
        "sciengine_direct",
        "biorxiv",
        "pmc_oa",
        "header_based",
    ]


def test_oa_mode_excludes_header_based():
    policy = fetch_cli._build_policy(_args("oa"), {})

    assert policy.mode == AccessMode.OA_ONLY
    assert "header_based" not in policy.enabled_resolver_names()


def test_header_based_available_with_doi_defaults():
    policy = fetch_cli._build_policy(_args("header-based"), {})

    assert policy.enabled_resolver_names() == ["header_based"]
    assert policy.extra["base_url"] == ""
    assert policy.extra["url_template"] == ""


def test_no_scihub_provider_or_url():
    assert "scihub" not in RESOLVER_REGISTRY
    for mode in AccessMode:
        assert "scihub" not in AccessPolicy(mode=mode).enabled_resolver_names()
