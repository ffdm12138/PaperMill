from src.discovery.models import PaperCandidate
from src.discovery.relevance import evaluate_candidate
from tests.helpers.relevance_profiles import relevance_profile


def test_hyphen_and_space_are_equivalent_but_token_boundaries_remain_strict():
    profile = relevance_profile(object_term="wind-blown sand")
    spaced = evaluate_candidate(
        PaperCandidate(
            title="Wind blown sand transport",
            raw={"topics": [{"subfield": {"id": "https://openalex.org/subfields/S1"}}]},
        ),
        profile, provider="openalex",
    )
    assert spaced.state != "rejected"

    reverse = relevance_profile(object_term="wind blown sand")
    hyphenated = evaluate_candidate(
        PaperCandidate(
            title="Wind-blown sand transport",
            raw={"topics": [{"subfield": {"id": "https://openalex.org/subfields/S1"}}]},
        ),
        reverse, provider="openalex",
    )
    assert hyphenated.state != "rejected"

    strict = relevance_profile(object_term="pbl")
    decision = evaluate_candidate(
        PaperCandidate(title="pblx transport"), strict, provider="crossref",
        scope_verifier=lambda *_: None,
    )
    assert decision.state == "rejected"
