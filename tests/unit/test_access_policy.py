"""Access policy unit tests — mode behavior, resolver chain, Sci-Hub guard."""
from __future__ import annotations

import pytest

from src.fetch.access_policy import AccessMode, AccessPolicy


pytestmark = pytest.mark.unit


# ── Default mode ──────────────────────────────────────────────────────

def test_access_policy_default_is_oa_only_with_original_link_first():
    policy = AccessPolicy()
    assert policy.mode == AccessMode.OA_ONLY
    enabled = policy.enabled_resolver_names()
    assert enabled[0] == "original_link"
    assert enabled == [
        "original_link",
        "unpaywall", "openalex", "semantic_scholar", "arxiv",
        "publisher_oa", "springer_direct",
        "sciengine_direct",
        "biorxiv", "pmc_oa",
    ]


# ── OA_ONLY excludes non-OA resolvers ──────────────────────────────────

@pytest.mark.parametrize("name", [
    "publisher_tdm",
    "institutional_browser",
    "browser_assisted",
    "local_manual",
    "scihub",
    "header_based",
    "wiley_tdm",
    "elsevier_tdm",
])
def test_oa_only_excludes_non_oa_resolvers(name):
    enabled = AccessPolicy(mode=AccessMode.OA_ONLY).enabled_resolver_names()
    assert name not in enabled


# ── Sci-Hub never enabled by any mode ──────────────────────────────────

@pytest.mark.parametrize("mode", list(AccessMode))
def test_access_policy_never_enables_scihub(mode):
    assert "scihub" not in AccessPolicy(mode=mode).enabled_resolver_names()


# ── CUSTOM mode TDM control ────────────────────────────────────────────

def test_access_policy_custom_tdm_controlled_by_flag():
    enabled = AccessPolicy(
        mode=AccessMode.CUSTOM, allow_publisher_tdm=True,
    ).enabled_resolver_names()
    assert "wiley_tdm" in enabled
    assert "elsevier_tdm" in enabled

    disabled = AccessPolicy(
        mode=AccessMode.CUSTOM, allow_publisher_tdm=False,
    ).enabled_resolver_names()
    assert "wiley_tdm" not in disabled
    assert "elsevier_tdm" not in disabled


# ── INSTITUTIONAL extras ──────────────────────────────────────────────

def test_access_policy_institutional_includes_extra_resolvers():
    enabled = AccessPolicy(mode=AccessMode.INSTITUTIONAL).enabled_resolver_names()
    for name in ["unpaywall", "publisher_tdm", "institutional_browser",
                 "wiley_tdm", "elsevier_tdm"]:
        assert name in enabled


# ── BROWSER_ASSISTED ───────────────────────────────────────────────────

def test_access_policy_browser_assisted_adds_only_browser():
    enabled = AccessPolicy(mode=AccessMode.BROWSER_ASSISTED).enabled_resolver_names()
    assert "browser_assisted" in enabled
    assert "publisher_tdm" not in enabled


# ── LOCAL_MANUAL ──────────────────────────────────────────────────────

def test_access_policy_local_manual_only():
    assert AccessPolicy(mode=AccessMode.LOCAL_MANUAL).enabled_resolver_names() == [
        "local_manual",
    ]


# ── CUSTOM resolver flag ──────────────────────────────────────────────

def test_access_policy_custom_resolvers_require_flag():
    disabled = AccessPolicy(
        mode=AccessMode.CUSTOM,
        allow_custom_resolvers=False,
        custom_resolvers=["my_resolver"],
    ).enabled_resolver_names()
    assert "my_resolver" not in disabled

    enabled = AccessPolicy(
        mode=AccessMode.CUSTOM,
        allow_custom_resolvers=True,
        custom_resolvers=["my_resolver"],
    ).enabled_resolver_names()
    assert "my_resolver" in enabled


# ── Explicit resolver names override ──────────────────────────────────

def test_access_policy_explicit_resolver_names_selects_header_based_only():
    policy = AccessPolicy(
        mode=AccessMode.CUSTOM,
        allow_custom_resolvers=True,
        custom_resolvers=["header_based"],
        extra={"resolver_names": ["header_based"]},
    )
    assert policy.enabled_resolver_names() == ["header_based"]


# ── clone_with ────────────────────────────────────────────────────────

def test_access_policy_clone_with_does_not_mutate_original():
    original = AccessPolicy(mode=AccessMode.OA_ONLY)
    cloned = original.clone_with(mode=AccessMode.INSTITUTIONAL)
    assert cloned.mode == AccessMode.INSTITUTIONAL
    assert original.mode == AccessMode.OA_ONLY
