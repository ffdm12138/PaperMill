"""Contract tests for pagination filter convergence (Phase 5).

Verifies that the half-implemented ``filters`` parameters have been removed from
the public signature contract: ``composite_backfill_signature`` and
``pagination_signature`` only accept fields that actually reach the provider
request. When filters are added later they must be wired end-to-end in one
change (CLI → DiscoveryOptions → provider request → signatures → tests).
"""
from __future__ import annotations

import inspect

import pytest

from src.discovery.keyword_notebook import (
    PAGINATION_SCHEMA_VERSION,
    composite_backfill_signature,
    pagination_signature,
)


pytestmark = pytest.mark.contract


def test_composite_backfill_signature_has_no_filter_params():
    sig = inspect.signature(composite_backfill_signature)
    params = set(sig.parameters)
    assert "openalex_backfill_filters" not in params
    assert "crossref_backfill_filters" not in params
    # The fields that DO affect the request/cursor are still present.
    assert {"page_size", "openalex_backfill_sort", "crossref_backfill_sort",
            "schema_version"} <= params


def test_pagination_signature_has_no_filter_param():
    sig = inspect.signature(pagination_signature)
    params = set(sig.parameters)
    assert "filters" not in params
    assert {"sort", "page_size", "schema_version"} <= params


def test_composite_signature_changes_with_sort():
    sig1 = composite_backfill_signature(
        page_size=50, openalex_backfill_sort="cited_by_count:desc",
        crossref_backfill_sort="published",
    )
    sig2 = composite_backfill_signature(
        page_size=50, openalex_backfill_sort="relevance" + "_score:desc",
        crossref_backfill_sort="published",
    )
    assert sig1 != sig2


def test_composite_signature_changes_with_page_size():
    sig1 = composite_backfill_signature(
        page_size=25, openalex_backfill_sort="cited_by_count:desc",
    )
    sig2 = composite_backfill_signature(
        page_size=50, openalex_backfill_sort="cited_by_count:desc",
    )
    assert sig1 != sig2


def test_composite_signature_stable_for_same_params():
    sig1 = composite_backfill_signature(
        page_size=50, openalex_backfill_sort="cited_by_count:desc",
        crossref_backfill_sort="published",
    )
    sig2 = composite_backfill_signature(
        page_size=50, openalex_backfill_sort="cited_by_count:desc",
        crossref_backfill_sort="published",
    )
    assert sig1 == sig2


def test_pagination_signature_changes_with_sort():
    assert pagination_signature(sort="relevance") != pagination_signature(sort="date")


def test_pagination_signature_stable_for_same_sort():
    assert pagination_signature(sort="relevance") == pagination_signature(sort="relevance")


def test_composite_backfill_signature_rejects_filter_kwargs():
    """Passing removed filter params must raise, not be silently ignored."""
    with pytest.raises(TypeError):
        composite_backfill_signature(
            page_size=50, openalex_backfill_filters=("foo", "bar"),
        )
    with pytest.raises(TypeError):
        composite_backfill_signature(
            page_size=50, crossref_backfill_filters=("foo", "bar"),
        )


def test_pagination_signature_rejects_filter_kwarg():
    with pytest.raises(TypeError):
        pagination_signature(sort="relevance", filters=("foo",))  # type: ignore[call-arg]
