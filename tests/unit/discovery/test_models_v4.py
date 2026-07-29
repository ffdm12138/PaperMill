"""Unit tests for v4 discovery contracts (canonical source: src.discovery.contracts)."""
from __future__ import annotations

import pytest

from src.discovery.contracts.page_journal import (
    PAGE_SCHEMA_VERSION_V4,
    PAGE_V4_FIELDS,
    ProviderPageJournalV4,
    UnexpectedNonV4StateError,
)
from src.discovery.contracts.manifest import (
    STORE_SCHEMA_VERSIONS_V4,
    DiscoveryWorkspaceManifestV4,
)


# ── DiscoveryWorkspaceManifestV4 ──────────────────────────────────────────


class TestWorkspaceManifestV4:
    def test_default_construction(self):
        # No construction path yields a partially-valid manifest: the
        # defaults are placeholders and must fail closed.
        with pytest.raises(ValueError):
            DiscoveryWorkspaceManifestV4()

    def test_rejects_wrong_schema(self):
        with pytest.raises(ValueError, match="schema_version"):
            DiscoveryWorkspaceManifestV4(
                schema_version="3.0",
                generation_id="v4-test",
                migration_id="mig-001",
                created_at="2026-07-23T00:00:00+00:00",
                completed_at="2026-07-23T00:00:00+00:00",
                store_schema_versions=dict(STORE_SCHEMA_VERSIONS_V4),
                workspace_tree_sha256="a" * 64,
            )

    def test_to_dict_roundtrip(self):
        m = DiscoveryWorkspaceManifestV4(
            generation_id="v4-test",
            created_at="2026-07-23T00:00:00+00:00",
            completed_at="2026-07-23T00:00:00+00:00",
            migration_id="mig-001",
            store_schema_versions=dict(STORE_SCHEMA_VERSIONS_V4),
            workspace_tree_sha256="a" * 64,
        )
        d = m.to_dict()
        assert d["generation_id"] == "v4-test"
        assert DiscoveryWorkspaceManifestV4.from_dict_strict(d) == m


# ── ProviderPageJournalV4 ─────────────────────────────────────────────────


class TestProviderPageJournalV4:
    def test_basic_construction(self):
        pj = ProviderPageJournalV4()
        assert pj.schema_version == "4.0"
        assert pj.returned_count == 0
        assert pj.candidates == ()
        assert pj.provider_exhausted is False

    def test_returned_count_must_match_candidates(self):
        with pytest.raises(ValueError, match="returned_count"):
            ProviderPageJournalV4(returned_count=5, candidates=())

    def test_returned_count_matches(self):
        pj = ProviderPageJournalV4(
            returned_count=2,
            candidates=({"doi": "10.1234/a"}, {"doi": "10.1234/b"}),
        )
        assert pj.returned_count == 2

    def test_exhausted_requires_evidence(self):
        with pytest.raises(ValueError, match="exhausted page requires"):
            ProviderPageJournalV4(provider_exhausted=True, exhaustion_evidence=None)

    def test_exhausted_rejects_next_cursor(self):
        evidence = {"provider": "openalex", "query_id": "q1",
                     "request_signature": "abc1234567890abc",
                     "generation": 1, "cursor_before": "*",
                     "response_metadata": {"http_status": 200, "next_cursor_present": False,
                                          "response_fingerprint": "fp", "observed_at": "2026-07-23T00:00:00Z"},
                     "observed_at": "2026-07-23T00:00:00Z"}
        with pytest.raises(ValueError, match="next_cursor=None"):
            ProviderPageJournalV4(
                provider_exhausted=True, next_cursor="still-has-cursor",
                exhaustion_evidence=evidence,
            )

    def test_exhausted_with_evidence_no_next_cursor_succeeds(self):
        evidence = {"provider": "openalex", "query_id": "q1",
                     "request_signature": "abc1234567890abc",
                     "generation": 1, "cursor_before": "*",
                     "response_metadata": {"http_status": 200, "next_cursor_present": False,
                                          "response_fingerprint": "fp", "observed_at": "2026-07-23T00:00:00Z"},
                     "observed_at": "2026-07-23T00:00:00Z"}
        pj = ProviderPageJournalV4(
            provider_exhausted=True, next_cursor=None,
            exhaustion_evidence=evidence,
        )
        assert pj.provider_exhausted is True
        assert pj.next_cursor is None

    def test_non_exhausted_rejects_evidence(self):
        evidence = {"provider": "openalex", "query_id": "q1",
                     "request_signature": "abc1234567890abc",
                     "generation": 1, "cursor_before": "*",
                     "response_metadata": {"http_status": 200, "next_cursor_present": False,
                                          "response_fingerprint": "fp", "observed_at": "2026-07-23T00:00:00Z"},
                     "observed_at": "2026-07-23T00:00:00Z"}
        with pytest.raises(ValueError, match="non-exhausted"):
            ProviderPageJournalV4(
                provider_exhausted=False, exhaustion_evidence=evidence,
            )

    def test_to_dict_roundtrip(self):
        evidence = {"provider": "openalex", "query_id": "q1",
                     "request_signature": "abc1234567890abc",
                     "generation": 1, "cursor_before": "*",
                     "response_metadata": {"http_status": 200, "next_cursor_present": False,
                                          "response_fingerprint": "fp", "observed_at": "2026-07-23T00:00:00Z"},
                     "observed_at": "2026-07-23T00:00:00Z"}
        pj = ProviderPageJournalV4(
            page_id="pg-1", keyword_id="k1", keyword_zh="test",
            query_id="q1", query="test query", query_language="zh",
            provider="openalex", lane="backfill", generation=1,
            request_cursor="*", next_cursor=None,
            provider_exhausted=True, returned_count=0,
            exhaustion_evidence=evidence,
            state="cursor_committed",
            fetched_at="2026-07-23T00:00:00Z",
            cursor_committed_at="2026-07-23T00:00:01Z",
            checksum="",
        )
        d = pj.to_dict()
        recovered = ProviderPageJournalV4.from_dict_strict(d)
        assert recovered.page_id == "pg-1"
        assert recovered.provider_exhausted is True
        assert recovered.state == "cursor_committed"

    def test_from_dict_strict_rejects_missing_fields(self):
        d = {"schema_version": "4.0"}  # missing everything
        with pytest.raises(ValueError, match="missing fields"):
            ProviderPageJournalV4.from_dict_strict(d)

    def test_from_dict_strict_rejects_unknown_fields(self):
        pj = ProviderPageJournalV4(
            page_id="pg-1", keyword_id="k1", keyword_zh="test",
            query_id="q1", query="test", query_language="zh",
            provider="openalex", lane="backfill",
        )
        d = pj.to_dict()
        d["unknown_key"] = "intruder"
        with pytest.raises(ValueError, match="unknown fields"):
            ProviderPageJournalV4.from_dict_strict(d)


# ── PAGE_V4_FIELDS completeness ───────────────────────────────────────────


class TestPageV4Fields:
    def test_required_fields_present(self):
        """Every field in PAGE_V4_FIELDS must be accepted by ProviderPageJournalV4.to_dict()."""
        pj = ProviderPageJournalV4()
        d = pj.to_dict()
        for field in PAGE_V4_FIELDS:
            assert field in d, f"PAGE_V4_FIELDS entry {field!r} not in to_dict()"
