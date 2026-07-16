import json
from pathlib import Path

import pytest

from scripts import compare_discovery_relevance
from scripts.compare_discovery_relevance import (
    _group_metrics,
    _load_groups,
    _resolve_group_profile_maps,
)
from src.discovery.keyword_notebook import KeywordNotebookStore
from src.discovery.models import PaperCandidate
from tests.helpers.relevance_profiles import relevance_profile


def test_comparison_rejects_fake_abc_copy(tmp_path: Path):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({
        "profiles": [{"keyword_zh": "风沙动力学", "profile": relevance_profile()}]
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="distinct groups"):
        _load_groups(path)


def test_comparison_report_exposes_required_metrics():
    metrics = _group_metrics([], set())
    assert {
        "precision_at_50", "cross_disciplinary_noise_rate",
        "strong_relevance_count", "new_paper_share", "rejection_reasons", "top_50",
    }.issubset(metrics)
    assert metrics["precision_at_50"] is None


def test_comparison_has_one_shared_fetch_phase_for_all_replay_groups():
    assert hasattr(compare_discovery_relevance, "fetch_shared_corpus")


def test_shared_fetch_calls_each_sampling_key_once_not_once_per_arm(tmp_path: Path):
    calls = []

    def fake_fetch(*, key, sampling_profile):
        calls.append((key["provider"], key["query_id"]))
        return [PaperCandidate(title="aeolian sand saltation", doi="10.1/shared")]

    sampling = {
        "subfield_union": ["S1"], "provider_sort": {
            "openalex": "relevance" + "_" + "score:desc"},
        "queries": ["aeolian"], "lanes": ["refresh"], "time_window": {},
        "budgets": [{
            "keyword_id": "kid", "provider": "openalex", "lane": "refresh",
            "query_id": "qid", "query": "aeolian", "target": 1,
        }],
    }
    compare_discovery_relevance.fetch_shared_corpus(
        sampling_profile=sampling,
        replay_profiles={arm: {"kid": f"hash-{arm}"} for arm in "ABC"},
        output_root=tmp_path,
        provider_fetchers={"openalex": fake_fetch},
    )
    assert calls == [("openalex", "qid")]
    manifest = json.loads((tmp_path / "corpus_manifest.json").read_text(encoding="utf-8"))
    assert manifest["budgets"][0]["actual"] == 1


def test_crossref_scope_evidence_is_fetched_once_in_batches_of_at_most_100(tmp_path: Path):
    candidates = [PaperCandidate(title=f"P {index}", doi=f"10.2/{index}")
                  for index in range(205)]
    batches = []

    def fetch_dois(dois):
        batches.append(list(dois))
        return {doi: {"topics": [{"subfield": {"id": "S1"}}]} for doi in dois}

    sampling = {
        "subfield_union": ["S1"], "provider_sort": {"crossref": "relevance"},
        "queries": ["q"], "lanes": ["refresh"], "time_window": {},
        "budgets": [{"keyword_id": "kid", "provider": "crossref",
                     "lane": "refresh", "query_id": "qid", "query": "q",
                     "target": 205}],
    }
    compare_discovery_relevance.fetch_shared_corpus(
        sampling_profile=sampling,
        replay_profiles={arm: {"kid": f"hash-{arm}"} for arm in "ABC"},
        output_root=tmp_path,
        provider_fetchers={"crossref": lambda **_kwargs: candidates},
        doi_evidence_fetcher=fetch_dois,
    )
    assert [len(batch) for batch in batches] == [100, 100, 5]


def test_replay_profile_labels_resolve_to_corpus_keyword_ids(tmp_path: Path):
    notebook = KeywordNotebookStore(tmp_path / "notebooks")
    created = notebook.create_notebook("风沙动力学", enabled=False)
    groups = {
        arm: {"profile_map": {"风沙动力学": relevance_profile(object_term=f"sand {arm}")}}
        for arm in "ABC"
    }
    resolved = _resolve_group_profile_maps(groups, tmp_path / "notebooks")
    assert all(set(resolved[arm]) == {created["keyword_id"]} for arm in "ABC")
