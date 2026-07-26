"""Unit tests for src.fetch.openalex_credentials."""
from __future__ import annotations

import pytest

from src.fetch.openalex_credentials import (
    OpenAlexCredentials,
    load_openalex_credentials,
    safe_request_error_summary,
)

pytestmark = pytest.mark.unit


class TestLoadOpenalexCredentials:
    """load_openalex_credentials() contract tests."""

    def test_loads_from_environment(self, monkeypatch):
        monkeypatch.setenv("OPENALEX_EMAIL", "user@example.org")
        monkeypatch.setenv("OPENALEX_API_KEY", "secr-etkey-12345")
        creds = load_openalex_credentials()
        assert creds.email == "user@example.org"
        assert creds.api_key == "secr-etkey-12345"
        assert creds.email_configured is True
        assert creds.api_key_configured is True

    def test_missing_returns_none(self, monkeypatch):
        monkeypatch.delenv("OPENALEX_EMAIL", raising=False)
        monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
        creds = load_openalex_credentials()
        assert creds.email is None
        assert creds.api_key is None
        assert creds.email_configured is False
        assert creds.api_key_configured is False

    def test_partial_email_only(self, monkeypatch):
        monkeypatch.setenv("OPENALEX_EMAIL", "only-email@test.org")
        monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
        creds = load_openalex_credentials()
        assert creds.email == "only-email@test.org"
        assert creds.api_key is None
        assert creds.email_configured is True
        assert creds.api_key_configured is False

    def test_partial_api_key_only(self, monkeypatch):
        monkeypatch.delenv("OPENALEX_EMAIL", raising=False)
        monkeypatch.setenv("OPENALEX_API_KEY", "only-apikey-999")
        creds = load_openalex_credentials()
        assert creds.email is None
        assert creds.api_key == "only-apikey-999"
        assert creds.email_configured is False
        assert creds.api_key_configured is True

    def test_empty_string_is_none(self, monkeypatch):
        monkeypatch.setenv("OPENALEX_EMAIL", "")
        monkeypatch.setenv("OPENALEX_API_KEY", "")
        creds = load_openalex_credentials()
        assert creds.email is None
        assert creds.api_key is None

    def test_whitespace_string_is_none(self, monkeypatch):
        monkeypatch.setenv("OPENALEX_EMAIL", "   ")
        monkeypatch.setenv("OPENALEX_API_KEY", "\t\n")
        creds = load_openalex_credentials()
        assert creds.email is None
        assert creds.api_key is None

    def test_whitespace_trimmed(self, monkeypatch):
        monkeypatch.setenv("OPENALEX_EMAIL", "  user@example.org  ")
        monkeypatch.setenv("OPENALEX_API_KEY", "  key-value-123  ")
        creds = load_openalex_credentials()
        assert creds.email == "user@example.org"
        assert creds.api_key == "key-value-123"

    def test_explicit_env_mapping(self):
        env = {"OPENALEX_EMAIL": "mapped@test.org", "OPENALEX_API_KEY": "mapped-key-456"}
        creds = load_openalex_credentials(env=env)
        assert creds.email == "mapped@test.org"
        assert creds.api_key == "mapped-key-456"

    def test_empty_dict_does_not_fall_back_to_real_env(self, monkeypatch):
        """An empty env dict must NOT fall through to os.environ."""
        monkeypatch.setenv("OPENALEX_EMAIL", "real@should-not-appear.com")
        monkeypatch.setenv("OPENALEX_API_KEY", "real-key-should-not-appear")
        creds = load_openalex_credentials(env={})
        assert creds.email is None
        assert creds.api_key is None


class TestOpenAlexCredentialsDataclass:
    """OpenAlexCredentials dataclass contract tests."""

    def test_safe_summary_no_leak(self):
        creds = OpenAlexCredentials(email="secret@leak.com", api_key="leaked-key-12345")
        summary = creds.safe_summary()
        assert "secret@leak.com" not in summary
        assert "leaked-key-12345" not in summary
        assert "email=yes" in summary
        assert "api_key=yes" in summary

    def test_repr_no_leak(self):
        creds = OpenAlexCredentials(email="repr@leak.com", api_key="repr-leak-99999")
        r = repr(creds)
        assert "repr@leak.com" not in r
        assert "repr-leak-99999" not in r
        assert "email=yes" in r
        assert "api_key=yes" in r

    def test_empty_creds_repr_and_summary(self):
        creds = OpenAlexCredentials()
        assert "email=no" in repr(creds)
        assert "api_key=no" in repr(creds)
        assert "email=no" in creds.safe_summary()
        assert "api_key=no" in creds.safe_summary()


class TestSafeRequestErrorSummary:
    def test_with_status_code(self):
        class FakeResponse:
            status_code = 429

        class FakeException(Exception):
            response = FakeResponse()

        exc = FakeException("https://api.openalex.org/works?mailto=user@example.org")
        result = safe_request_error_summary(exc)
        assert result == "FakeException (HTTP 429)"
        # Must NOT contain the URL or mailto
        assert "mailto" not in result
        assert "example.org" not in result

    def test_without_status_code(self):
        exc = ValueError("generic error")
        result = safe_request_error_summary(exc)
        assert result == "ValueError"
