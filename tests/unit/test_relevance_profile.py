import pytest

from src.discovery.contracts.notebook import (
    NotebookCorruptError,
    empty_notebook,
    validate_discovery_readiness,
    validate_notebook,
)
from src.discovery.models import PaperCandidate
from src.discovery.relevance import (
    MATCHER_SCHEMA_VERSION,
    RELEVANCE_PROFILE_SCHEMA_VERSION,
    RelevanceProfileError,
    evaluate_candidate,
    validate_relevance_profile,
    validate_relevance_profile_source,
)
from tests.helpers.relevance_profiles import (
    relevance_profile,
    retired_sentinel_profile,
)


def _bilingual_queries(keyword_zh: str, en_query: str) -> dict:
    return {
        "a": {"active": True, "query": keyword_zh, "language": "zh"},
        "b": {"active": True, "query": en_query, "language": "en"},
    }


def _unresolved_profile() -> dict:
    """Source-valid profile that is not taxonomy-resolved."""
    return {
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


def test_empty_notebook_is_disabled_draft_with_unconfigured_profile():
    notebook = empty_notebook("风沙动力学")
    assert notebook["enabled"] is False
    assert notebook["relevance_profile"] is None
    # A disabled draft without a profile is still structurally valid.
    validate_notebook(dict(notebook))
    readiness = validate_discovery_readiness(notebook)
    assert not readiness.ready
    assert any("disabled" in error for error in readiness.errors)


def test_enabled_notebook_without_profile_fails_readiness_closed():
    notebook = empty_notebook("风沙动力学")
    notebook["enabled"] = True
    notebook["search_queries"] = _bilingual_queries("风沙动力学", "aeolian dynamics")
    readiness = validate_discovery_readiness(notebook)
    assert not readiness.ready
    assert any(
        "unconfigured" in error and "configure_relevance_profiles" in error
        for error in readiness.errors
    )


def test_retired_sentinel_profile_fails_notebook_validation():
    """A notebook still carrying the removed sentinel fails closed."""
    for enabled in (False, True):
        notebook = empty_notebook("风沙动力学")
        notebook["enabled"] = enabled
        notebook["relevance_profile"] = retired_sentinel_profile()
        with pytest.raises(NotebookCorruptError, match="profile_unbound sentinel"):
            validate_notebook(notebook)


def test_retired_sentinel_profile_is_not_discovery_ready_or_evaluable():
    notebook = empty_notebook("风沙动力学")
    notebook["enabled"] = True
    notebook["search_queries"] = _bilingual_queries("风沙动力学", "aeolian dynamics")
    notebook["relevance_profile"] = retired_sentinel_profile()
    readiness = validate_discovery_readiness(notebook)
    assert not readiness.ready
    with pytest.raises(RelevanceProfileError, match="resolve OpenAlex subfields"):
        evaluate_candidate(
            PaperCandidate(title="Semiconductor electrochemistry"),
            notebook["relevance_profile"],
            provider="crossref",
        )


def test_unresolved_profile_fails_readiness():
    """An unresolved (resolved=False) profile with real filter_labels
    must fail discovery readiness with the active validator."""
    notebook = empty_notebook("风沙动力学")
    notebook["enabled"] = True
    notebook["search_queries"] = _bilingual_queries("风沙动力学", "aeolian dynamics")
    notebook["relevance_profile"] = _unresolved_profile()
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
    notebook["enabled"] = True
    notebook["search_queries"] = _bilingual_queries("电化学", "electrochemistry")
    notebook["relevance_profile"] = relevance_profile()
    readiness = validate_discovery_readiness(notebook)
    assert readiness.ready
    assert not readiness.errors


def test_source_valid_but_active_rejected_fails_readiness():
    """Source validator accepts it, but active validator rejects it —
    readiness must fail independently."""
    notebook = empty_notebook("电化学")
    notebook["enabled"] = True
    notebook["search_queries"] = _bilingual_queries("电化学", "electrochemistry")
    notebook["relevance_profile"] = _unresolved_profile()
    # Source validator accepts unresolved profiles.
    validate_relevance_profile_source(notebook["relevance_profile"])
    # But readiness calls the active validator → must fail.
    readiness = validate_discovery_readiness(notebook)
    assert not readiness.ready
    assert any("active-ready" in error for error in readiness.errors)
