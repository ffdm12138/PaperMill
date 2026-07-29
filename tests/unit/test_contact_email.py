"""Tests for contact-email resolution.

A placeholder address is worse than none: Unpaywall answers HTTP 422 for
reserved domains, so every request sent with one is a guaranteed failure that
still consumes the request budget.
"""
import pytest

from src.utils.contact_email import (
    CONTACT_EMAIL_ENV_VARS,
    contact_email_safe_summary,
    is_usable_contact_email,
    load_contact_email,
)


# ── 1. precedence ─────────────────────────────────────────────────────

def test_dedicated_variable_wins():
    env = {
        "MINERU_CONTACT_EMAIL": "first@lab.uni.edu",
        "MINERU_METADATA_CONTACT_EMAIL": "second@lab.uni.edu",
    }
    assert load_contact_email(env) == "first@lab.uni.edu"


def test_falls_through_to_later_variables():
    assert load_contact_email({"MINERU_METADATA_CONTACT_EMAIL": "only@lab.uni.edu"}) == "only@lab.uni.edu"


def test_unusable_value_falls_through_to_the_next_variable():
    env = {"MINERU_CONTACT_EMAIL": "anonymous@example.com",
           "MINERU_METADATA_CONTACT_EMAIL": "real@lab.uni.edu"}
    assert load_contact_email(env) == "real@lab.uni.edu"


def test_provider_credential_names_are_not_consulted():
    """Credential variable names stay owned by their own module; a caller that
    wants to reuse one composes the lookups instead."""
    assert load_contact_email({"OPENALEX_EMAIL": "cred@lab.uni.edu"}) is None


# ── 2. an empty mapping is "not configured", not "read os.environ" ────

def test_empty_env_does_not_fall_back_to_process_environment(monkeypatch):
    monkeypatch.setenv("MINERU_CONTACT_EMAIL", "ambient@lab.uni.edu")
    assert load_contact_email({}) is None


def test_none_env_reads_process_environment(monkeypatch):
    for name in CONTACT_EMAIL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MINERU_CONTACT_EMAIL", "ambient@lab.uni.edu")
    assert load_contact_email() == "ambient@lab.uni.edu"


# ── 3. placeholder rejection ──────────────────────────────────────────

@pytest.mark.parametrize("address", [
    "anonymous@example.com",
    "bot@example.org",
    "a@example.net",
    "a@example.edu",
    "a@localhost",
    "a@invalid",
    "a@test",
    "A@EXAMPLE.COM",
])
def test_reserved_domains_are_rejected(address):
    assert is_usable_contact_email(address) is False
    assert load_contact_email({"MINERU_CONTACT_EMAIL": address}) is None


@pytest.mark.parametrize("address", [
    "", "   ", "no-at-sign", "two@at@signs.com", "@nolocal.com",
    "nodomain@", "has space@lab.uni.edu", "a@nodot",
])
def test_malformed_addresses_are_rejected(address):
    assert is_usable_contact_email(address) is False


@pytest.mark.parametrize("address", [
    "220220935741@lzu.edu.cn",
    "researcher@uni.edu",
    "first.last+tag@sub.department.example.ac.uk",
])
def test_real_addresses_are_accepted(address):
    assert is_usable_contact_email(address) is True


def test_surrounding_whitespace_is_tolerated():
    assert load_contact_email({"MINERU_CONTACT_EMAIL": "  real@lab.uni.edu \n"}) == "real@lab.uni.edu"


# ── 4. the summary never leaks the address ────────────────────────────

def test_safe_summary_omits_the_address():
    summary = contact_email_safe_summary({"MINERU_CONTACT_EMAIL": "secret@lab.uni.edu"})
    assert "secret" not in summary and "lab.uni.edu" not in summary
    assert summary.endswith("yes")
    assert contact_email_safe_summary({}).endswith("no")
