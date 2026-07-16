import pytest

from src.discovery.keyword_notebook import empty_notebook, validate_discovery_readiness
from src.discovery.models import PaperCandidate
from src.discovery.relevance import RelevanceProfileError, evaluate_candidate


def test_enabled_legacy_profile_is_not_discovery_ready_or_evaluable():
    notebook = empty_notebook("风沙动力学")
    notebook["search_queries"] = {
        "a": {"active": True, "query": "风沙动力学", "language": "zh"},
        "b": {"active": True, "query": "aeolian dynamics", "language": "en"},
    }
    readiness = validate_discovery_readiness(notebook)
    assert not readiness.ready
    assert any("profile_unbound" in error for error in readiness.errors)
    with pytest.raises(RelevanceProfileError, match="profile_unbound"):
        evaluate_candidate(
            PaperCandidate(title="Semiconductor electrochemistry"),
            notebook["relevance_profile"],
            provider="crossref",
        )
