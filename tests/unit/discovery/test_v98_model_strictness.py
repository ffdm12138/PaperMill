"""Phase 0.7: Model strictness regression tests for v99.

Tests that invalid input values are rejected by model constructors.
On v99 baseline, some tests FAIL because strictness is incomplete
(e.g. bool→int coercion, empty request_signature, negative retry_after).
"""
from __future__ import annotations

import pytest

from src.discovery.execution.lane_models import (
    DiscoveryLaneKey,
    ExhaustionEvidence,
    ProviderResponseMetadata,
    RequestSignature,
)

pytestmark = pytest.mark.unit


# ── DiscoveryLaneKey strictness ────────────────────────────────────────


class TestDiscoveryLaneKeyStrictness:
    """DiscoveryLaneKey must reject invalid constructor values."""

    def _key(self, **overrides):
        kw = dict(
            keyword_id="k1", query_id="q1", provider="openalex",
            mode="backfill", generation=1,
            request_signature=RequestSignature.create(sort="", filters={}, page_size=50).hash,
        )
        kw.update(overrides)
        return DiscoveryLaneKey(**kw)

    def test_rejects_generation_true(self):
        """generation=True must be rejected (bool is not a valid generation)."""
        with pytest.raises((ValueError, TypeError)):
            self._key(generation=True)

    def test_rejects_generation_float(self):
        """generation=1.0 must be rejected."""
        with pytest.raises((ValueError, TypeError)):
            self._key(generation=1.0)

    def test_rejects_generation_negative(self):
        """generation=-1 must be rejected."""
        with pytest.raises((ValueError, TypeError)):
            self._key(generation=-1)

    def test_rejects_empty_keyword_id(self):
        """Empty keyword_id must be rejected."""
        with pytest.raises((ValueError, TypeError)):
            self._key(keyword_id="")

    def test_rejects_empty_query_id(self):
        """Empty query_id must be rejected."""
        with pytest.raises((ValueError, TypeError)):
            self._key(query_id="")

    def test_rejects_empty_request_signature(self):
        """Empty request_signature must be rejected."""
        with pytest.raises((ValueError, TypeError)):
            self._key(request_signature="")

    def test_rejects_invalid_provider(self):
        """Invalid provider must be rejected."""
        with pytest.raises((ValueError, TypeError)):
            self._key(provider="invalid")

    def test_rejects_invalid_mode(self):
        """Invalid mode must be rejected."""
        with pytest.raises((ValueError, TypeError)):
            self._key(mode="invalid")


# ── ProviderResponseMetadata strictness ────────────────────────────────


class TestProviderResponseMetadataStrictness:
    """ProviderResponseMetadata must reject invalid values."""

    def _meta(self, **overrides):
        kw = dict(
            http_status=200,
            provider_request_id=None,
            retry_after_observed=None,
            total_results=None,
            next_cursor_present=False,
            response_fingerprint="abc123def456",
            observed_at="2024-01-01T00:00:00+00:00",
        )
        kw.update(overrides)
        return ProviderResponseMetadata(**kw)

    def test_rejects_retry_after_negative(self):
        """retry_after_observed=-1 must be rejected."""
        with pytest.raises((ValueError, TypeError)):
            self._meta(retry_after_observed=-1)

    def test_rejects_total_results_negative(self):
        """total_results=-1 must be rejected."""
        with pytest.raises((ValueError, TypeError)):
            self._meta(total_results=-1)

    def test_rejects_http_status_out_of_range(self):
        """http_status=99 must be rejected."""
        with pytest.raises((ValueError, TypeError)):
            self._meta(http_status=99)

    def test_rejects_empty_observed_at(self):
        """Empty observed_at must be rejected."""
        with pytest.raises((ValueError, TypeError)):
            self._meta(observed_at="")

    def test_rejects_empty_response_fingerprint(self):
        """Empty response_fingerprint must be rejected."""
        with pytest.raises((ValueError, TypeError)):
            self._meta(response_fingerprint="")


# ── ExhaustionEvidence strictness ──────────────────────────────────────


class TestExhaustionEvidenceStrictness:
    """ExhaustionEvidence must be consistent with lane metadata."""

    def test_exhaustion_requires_no_next_cursor(self):
        """When exhausted=True, next_cursor_present must be False."""
        # ExhaustionEvidence constructor should validate this
        meta = ProviderResponseMetadata(
            http_status=200,
            response_fingerprint="abc123",
            observed_at="2024-01-01T00:00:00+00:00",
            next_cursor_present=True,  # inconsistent with exhaustion
            total_results=0,
        )
        # v99 baseline: may or may not reject; document behavior
        try:
            evidence = ExhaustionEvidence(
                provider="openalex",
                query="test",
                query_language="en",
                keyword_id="k1",
                provider_generation=1,
                request_signature=RequestSignature.create(sort="", filters={}, page_size=50).hash,
                cursor_before="*",
                response_metadata=meta,
            )
            # If constructed, exhaustion evidence with next_cursor=True is invalid
            assert not evidence.response_metadata.next_cursor_present, (
                "ExhaustionEvidence with next_cursor_present=True should be rejected"
            )
        except (ValueError, TypeError):
            # Rejection is the correct behavior
            pass

    def test_exhaustion_evidence_fields_match_lane(self):
        """ExhaustionEvidence provider/query_id/generation/signature must be consistent."""
        sig = RequestSignature.create(sort="published", filters={"q": "test"}, page_size=50)
        evidence = ExhaustionEvidence(
            provider="openalex",
            query_id="q1",
            generation=5,
            request_signature=sig.hash,
            cursor_before="*",
            observed_at="2024-01-01T00:00:00+00:00",
            response_metadata=ProviderResponseMetadata(
                http_status=200,
                response_fingerprint="abc123",
                observed_at="2024-01-01T00:00:00+00:00",
                next_cursor_present=False,
                total_results=0,
            ),
        )
        assert evidence.provider == "openalex"
        assert evidence.generation == 5
        assert evidence.request_signature == sig.hash
        assert evidence.cursor_before == "*"
        assert evidence.response_metadata.next_cursor_present is False
