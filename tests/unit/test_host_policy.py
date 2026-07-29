"""Tests for the ASN-blocked host policy."""
import pytest

from src.fetch.host_policy import (
    blocked_hosts_in,
    classify_failure,
    is_bot_blocked_host,
    normalize_host,
)


# ── 1. suffix matching covers subdomains, not lookalikes ──────────────

@pytest.mark.parametrize("url", [
    "https://www.mdpi.com/2071-1050/18/3/1645/pdf",
    "https://onlinelibrary.wiley.com/doi/pdfdirect/10.1002/esp.3310",
    "https://agupubs.onlinelibrary.wiley.com/doi/pdf/10.1029/x",
    "https://rmets.onlinelibrary.wiley.com/doi/pdf/10.1002/x",
    "https://www.sciencedirect.com/science/article/pii/S1",
    "https://pubs.aip.org/aip/pof/article/1/1/1",
])
def test_known_blocked_hosts(url):
    assert is_bot_blocked_host(url) is True


@pytest.mark.parametrize("url", [
    "https://acp.copernicus.org/articles/20/14801/2020/acp-20-14801-2020.pdf",
    "https://repositorio.ufc.br/bitstream/x.pdf",
    "https://arxiv.org/pdf/2101.00001.pdf",
    "https://journals.ametsoc.org/view/journals/hydr/5/5/x.xml",
    "https://www.nature.com/articles/x.pdf",
])
def test_hosts_that_do_serve_pdfs_are_not_blocked(url):
    """Hosts with recorded successes must never be blocklisted."""
    assert is_bot_blocked_host(url) is False


def test_lookalike_domains_do_not_match():
    """Suffix matching must respect label boundaries, not raw substrings."""
    assert is_bot_blocked_host("https://notmdpi.com/a.pdf") is False
    assert is_bot_blocked_host("https://mdpi.com.evil.example/a.pdf") is False
    assert is_bot_blocked_host("https://fakeonlinelibrary.wiley.com.attacker.net/x") is False


def test_doi_redirector_is_never_blocked():
    """A 403 recorded against doi.org belongs to whatever it redirected to."""
    assert is_bot_blocked_host("https://doi.org/10.3390/su18031645") is False
    assert is_bot_blocked_host("https://dx.doi.org/10.3390/su18031645") is False


# ── 2. host normalization ─────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("https://Journals.Example.ORG:443/a", "journals.example.org"),
    ("journals.example.org.", "journals.example.org"),
    ("www.mdpi.com", "www.mdpi.com"),
    ("", ""),
    ("not a url", ""),
])
def test_normalize_host(value, expected):
    assert normalize_host(value) == expected


def test_port_and_credentials_do_not_defeat_matching():
    assert is_bot_blocked_host("https://user:pw@www.mdpi.com:443/a/pdf") is True


# ── 3. blocked_hosts_in tolerates raw payloads ────────────────────────

def test_blocked_hosts_in_ignores_non_strings_and_dedupes():
    found = blocked_hosts_in([
        "https://www.mdpi.com/a/pdf",
        "https://www.mdpi.com/b/pdf",
        None, 42, {"url": "x"},
        "https://acp.copernicus.org/a.pdf",
    ])
    assert found == ["www.mdpi.com"]


def test_blocked_hosts_in_accepts_a_bare_string():
    assert blocked_hosts_in("https://www.mdpi.com/a/pdf") == ["www.mdpi.com"]


# ── 4. failure classification drives the operator worklist ────────────

def test_failure_touching_a_blocked_host_is_blocked_publisher():
    item = {
        "status": "failed",
        "transport_attempts": [
            {"request_url": "https://doi.org/10.3390/x", "final_url": "https://www.mdpi.com/x/pdf"},
        ],
    }
    assert classify_failure(item) == "blocked_publisher"


def test_failure_without_a_blocked_host_is_unresolved():
    item = {
        "status": "failed",
        "transport_attempts": [{"request_url": "https://acp.copernicus.org/x.pdf", "final_url": ""}],
    }
    assert classify_failure(item) == "unresolved"


def test_non_failures_are_not_classified():
    assert classify_failure({"status": "attached"}) == ""
    assert classify_failure({"status": "skipped"}) == ""


def test_classification_reads_resolver_attempts_too():
    item = {
        "status": "failed",
        "attempts": [{"candidate_url": "https://onlinelibrary.wiley.com/doi/pdf/10.1002/x"}],
    }
    assert classify_failure(item) == "blocked_publisher"
