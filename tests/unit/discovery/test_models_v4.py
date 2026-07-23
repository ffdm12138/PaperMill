"""Unit tests for v4 discovery contracts (canonical source: src.discovery.contracts)."""
from __future__ import annotations

import pytest

from src.discovery.constants import INITIAL_CURSOR
from src.discovery.contracts.candidate import (
    CANDIDATE_ORIGIN_VALUES,
    LegacyCandidateSeedV4,
    PendingCandidateV4,
)
from src.discovery.contracts.page_journal import (
    PAGE_SCHEMA_VERSION_V4,
    PAGE_V4_FIELDS,
    ProviderPageJournalV4,
    UnexpectedNonV4StateError,
)
from src.discovery.contracts.lane_state import (
    CursorTransactionV4,
    LaneStateV4,
)
from src.discovery.contracts.manifest import DiscoveryWorkspaceManifestV4


# ── DiscoveryWorkspaceManifestV4 ──────────────────────────────────────────


class TestWorkspaceManifestV4:
    def test_default_construction(self):
        m = DiscoveryWorkspaceManifestV4()
        assert m.schema_version == "4.0"

    def test_rejects_wrong_schema(self):
        with pytest.raises(ValueError, match="schema_version"):
            DiscoveryWorkspaceManifestV4(schema_version="3.0")

    def test_to_dict_roundtrip(self):
        m = DiscoveryWorkspaceManifestV4(
            generation_id="v4-test",
            created_at="2026-07-23T00:00:00Z",
            lane_count=20,
            migration_id="mig-001",
        )
        d = m.to_dict()
        assert d["generation_id"] == "v4-test"
        assert d["lane_count"] == 20


# ── LaneStateV4 (contracts: flat fields, not lane_key) ───────────────────


class TestLaneStateV4:
    def test_default_construction(self):
        ls = LaneStateV4(keyword_id="k1", query_id="q1", provider="openalex", mode="backfill")
        assert ls.cursor == INITIAL_CURSOR
        assert ls.exhausted is False
        assert ls.generation == 1
        assert ls.revision == 0
        assert ls.exhaustion_evidence_id is None

    def test_exhausted_requires_evidence(self):
        with pytest.raises(ValueError, match="exhausted.*requires"):
            LaneStateV4(keyword_id="k1", query_id="q1", provider="openalex", mode="backfill",
                        exhausted=True, exhaustion_evidence_id=None)

    def test_non_exhausted_rejects_evidence(self):
        with pytest.raises(ValueError, match="non-exhausted"):
            LaneStateV4(keyword_id="k1", query_id="q1", provider="openalex", mode="backfill",
                        exhausted=False, exhaustion_evidence_id="ev-001")

    def test_exhausted_with_evidence_constructs(self):
        ls = LaneStateV4(keyword_id="k1", query_id="q1", provider="openalex", mode="backfill",
                         exhausted=True, exhaustion_evidence_id="ev-001")
        assert ls.exhausted is True
        assert ls.exhaustion_evidence_id == "ev-001"

    def test_rejects_negative_generation(self):
        with pytest.raises(ValueError, match="generation"):
            LaneStateV4(keyword_id="k1", query_id="q1", provider="openalex", mode="backfill",
                        generation=0)

    def test_to_dict_roundtrip(self):
        ls = LaneStateV4(keyword_id="k1", query_id="q1", provider="openalex", mode="backfill",
                         cursor="next-page", exhausted=True,
                         exhaustion_evidence_id="ev-001", revision=3)
        d = ls.to_dict()
        assert d["schema_version"] == "4.0"
        assert d["cursor"] == "next-page"
        recovered = LaneStateV4.from_dict_strict(d)
        assert recovered.cursor == "next-page"
        assert recovered.exhausted is True
        assert recovered.revision == 3

    def test_from_dict_strict_rejects_unknown_fields(self):
        d = LaneStateV4(keyword_id="k1", query_id="q1", provider="openalex", mode="backfill").to_dict()
        d["extra_field"] = "nope"
        with pytest.raises(ValueError, match="unknown"):
            LaneStateV4.from_dict_strict(d)


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


# ── PendingCandidateV4 ────────────────────────────────────────────────────


class TestPendingCandidateV4:
    def test_default_origin_is_provider_page(self):
        pc = PendingCandidateV4()
        assert pc.origin == "provider_page"

    def test_rejects_invalid_origin(self):
        with pytest.raises(ValueError, match="invalid origin"):
            PendingCandidateV4(origin="bad_origin")  # type: ignore[arg-type]

    def test_all_origin_values_accepted(self):
        for origin in CANDIDATE_ORIGIN_VALUES:
            pc = PendingCandidateV4(origin=origin)  # type: ignore[arg-type]
            assert pc.origin == origin


# ── LegacyCandidateSeedV4 ─────────────────────────────────────────────────


class TestLegacyCandidateSeedV4:
    def test_basic_construction(self):
        seed = LegacyCandidateSeedV4(
            seed_id="s1", doi="10.1234/test",
            normalized_doi="10.1234/test",
            source_schema_version="2.0",
        )
        assert seed.seed_id == "s1"

    def test_compute_seed_id_deterministic(self):
        sid1 = LegacyCandidateSeedV4.compute_seed_id("page-1", "10.1234/a")
        sid2 = LegacyCandidateSeedV4.compute_seed_id("page-1", "10.1234/a")
        assert sid1 == sid2
        assert len(sid1) == 32

    def test_rejects_empty_seed_id(self):
        with pytest.raises(ValueError, match="seed_id"):
            LegacyCandidateSeedV4(seed_id="", doi="10.1234/x", normalized_doi="10.1234/x")

    def test_rejects_invalid_source_schema(self):
        with pytest.raises(ValueError, match="source_schema_version"):
            LegacyCandidateSeedV4(
                seed_id="s1", doi="10.1234/x", normalized_doi="10.1234/x",
                source_schema_version="4.0",
            )


# ── CursorTransactionV4 (contracts: flat fields, not lane_key) ────────────


class TestCursorTransactionV4:
    def test_valid_transaction(self):
        tx = CursorTransactionV4(
            keyword_id="k1", query_id="q1", provider="openalex", mode="backfill",
            expected_revision=0, expected_cursor=INITIAL_CURSOR,
            new_cursor="next-page", new_revision=1,
        )
        assert tx.new_revision == 1
        assert tx.new_cursor == "next-page"

    def test_rejects_new_revision_not_greater(self):
        with pytest.raises(ValueError, match="new_revision.* must be >"):
            CursorTransactionV4(
                keyword_id="k1", query_id="q1", provider="openalex", mode="backfill",
                expected_revision=1, new_revision=1,
                expected_cursor="*", new_cursor="next",
            )

    def test_rejects_negative_expected_revision(self):
        with pytest.raises(ValueError, match="expected_revision"):
            CursorTransactionV4(
                keyword_id="k1", query_id="q1", provider="openalex", mode="backfill",
                expected_revision=-1, expected_cursor="*",
                new_cursor="next", new_revision=0,
            )

    def test_rejects_empty_expected_cursor(self):
        with pytest.raises(ValueError, match="expected_cursor"):
            CursorTransactionV4(
                keyword_id="k1", query_id="q1", provider="openalex", mode="backfill",
                expected_revision=0, expected_cursor="",
                new_cursor="next", new_revision=1,
            )


# ── PAGE_V4_FIELDS completeness ───────────────────────────────────────────


class TestPageV4Fields:
    def test_required_fields_present(self):
        """Every field in PAGE_V4_FIELDS must be accepted by ProviderPageJournalV4.to_dict()."""
        pj = ProviderPageJournalV4()
        d = pj.to_dict()
        for field in PAGE_V4_FIELDS:
            assert field in d, f"PAGE_V4_FIELDS entry {field!r} not in to_dict()"
