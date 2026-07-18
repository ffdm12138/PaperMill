import pytest

from src.discovery.keyword_notebook import empty_notebook, validate_discovery_readiness
from src.discovery.models import PaperCandidate
from src.discovery.relevance import (
    MATCHER_SCHEMA_VERSION,
    RELEVANCE_PROFILE_SCHEMA_VERSION,
    RelevanceProfileError,
    evaluate_candidate,
    validate_relevance_profile,
    validate_relevance_profile_source,
)


def test_enabled_legacy_profile_is_not_discovery_ready_or_evaluable():
    notebook = empty_notebook("风沙动力学")
    notebook["search_queries"] = {
        "a": {"active": True, "query": "风沙动力学", "language": "zh"},
        "b": {"active": True, "query": "aeolian dynamics", "language": "en"},
    }
    readiness = validate_discovery_readiness(notebook)
    assert not readiness.ready
    assert any("profile_unbound" in error for error in readiness.errors)
    with pytest.raises(RelevanceProfileError, match="profile_unbound|resolve OpenAlex subfields"):
        evaluate_candidate(
            PaperCandidate(title="Semiconductor electrochemistry"),
            notebook["relevance_profile"],
            provider="crossref",
        )


def test_unresolved_non_legacy_profile_fails_readiness():
    """An unresolved (resolved=False) profile with real filter_labels
    must fail discovery readiness with the active validator."""
    notebook = empty_notebook("风沙动力学")
    notebook["search_queries"] = {
        "a": {"active": True, "query": "风沙动力学", "language": "zh"},
        "b": {"active": True, "query": "aeolian dynamics", "language": "en"},
    }
    # Replace the legacy unbound profile with a source-valid but unresolved one.
    notebook["relevance_profile"] = {
        "schema_version": RELEVANCE_PROFILE_SCHEMA_VERSION,
        "matcher_schema_version": MATCHER_SCHEMA_VERSION,
        "openalex": {
            "filter_level": "subfield",
            "resolved": False,
            "filter_ids": [],
            "filter_labels": ["Electrochemistry"],
            "refresh_sort": "publication_date:desc",
            "backfill_sort": "cited_by_count:desc",
        },
        "crossref": {
            "scope_policy": "require_openalex_subfield",
            "refresh_sort": "published",
            "refresh_order": "desc",
            "backfill_sort": "relevance",
            "backfill_order": "desc",
        },
        "anchors": {
            "required_groups": [
                {"name": "object", "terms": ["electrochem", "electrode"]},
                {"name": "process", "terms": ["oxidation", "reduction"]},
            ],
            "negative_any": [],
            "missing_abstract_policy": "require_all_groups_in_title",
        },
    }
    # Source validator should pass (structure is valid, just unresolved).
    validate_relevance_profile_source(notebook["relevance_profile"])
    # But readiness must call the active validator and fail.
    readiness = validate_discovery_readiness(notebook)
    assert not readiness.ready
    assert any(
        "active-ready" in error or "resolve" in error.lower()
        for error in readiness.errors
    )


def test_resolved_valid_profile_passes_readiness():
    """A fully resolved, valid profile must pass discovery readiness."""
    notebook = empty_notebook("电化学")
    notebook["search_queries"] = {
        "a": {"active": True, "query": "电化学", "language": "zh"},
        "b": {"active": True, "query": "electrochemistry", "language": "en"},
    }
    # Create a fully resolved active profile.
    notebook["relevance_profile"] = validate_relevance_profile({
        "schema_version": RELEVANCE_PROFILE_SCHEMA_VERSION,
        "matcher_schema_version": MATCHER_SCHEMA_VERSION,
        "openalex": {
            "filter_level": "subfield",
            "resolved": True,
            "filter_ids": ["T12345"],
            "filter_labels": ["Electrochemistry"],
            "refresh_sort": "publication_date:desc",
            "backfill_sort": "cited_by_count:desc",
        },
        "crossref": {
            "scope_policy": "require_openalex_subfield",
            "refresh_sort": "published",
            "refresh_order": "desc",
            "backfill_sort": "relevance",
            "backfill_order": "desc",
        },
        "anchors": {
            "required_groups": [
                {"name": "object", "terms": ["electrochem", "electrode"]},
                {"name": "process", "terms": ["oxidation", "reduction"]},
            ],
            "negative_any": [],
            "missing_abstract_policy": "require_all_groups_in_title",
        },
    })
    readiness = validate_discovery_readiness(notebook)
    assert readiness.ready
    assert not readiness.errors


def test_source_valid_but_active_rejected_fails_readiness():
    """Source validator accepts it, but active validator rejects it —
    readiness must fail independently."""
    notebook = empty_notebook("电化学")
    notebook["search_queries"] = {
        "a": {"active": True, "query": "电化学", "language": "zh"},
        "b": {"active": True, "query": "electrochemistry", "language": "en"},
    }
    # Empty filter_ids with resolved=True — source passes, active rejects.
    notebook["relevance_profile"] = {
        "schema_version": RELEVANCE_PROFILE_SCHEMA_VERSION,
        "matcher_schema_version": MATCHER_SCHEMA_VERSION,
        "openalex": {
            "filter_level": "subfield",
            "resolved": True,
            "filter_ids": [],
            "filter_labels": ["Electrochemistry"],
            "refresh_sort": "publication_date:desc",
            "backfill_sort": "cited_by_count:desc",
        },
        "crossref": {
            "scope_policy": "require_openalex_subfield",
            "refresh_sort": "published",
            "refresh_order": "desc",
            "backfill_sort": "relevance",
            "backfill_order": "desc",
        },
        "anchors": {
            "required_groups": [
                {"name": "object", "terms": ["electrochem", "electrode"]},
                {"name": "process", "terms": ["oxidation", "reduction"]},
            ],
            "negative_any": [],
            "missing_abstract_policy": "require_all_groups_in_title",
        },
    }
    # Source validator checks structure — should pass (resolved=True with empty
    # filter_ids is a structural issue caught by the active validator, not source).
    # Actually with resolved=True, source validator does enforce non-empty filter_ids.
    # Let's use a case where source passes but resolved=False blocks active.
    notebook["relevance_profile"]["openalex"]["resolved"] = False
    notebook["relevance_profile"]["openalex"]["filter_ids"] = []
    # Source validator accepts unresolved profiles.
    validate_relevance_profile_source(notebook["relevance_profile"])
    # But readiness calls the active validator → must fail.
    readiness = validate_discovery_readiness(notebook)
    assert not readiness.ready
    assert any("active-ready" in error for error in readiness.errors)
