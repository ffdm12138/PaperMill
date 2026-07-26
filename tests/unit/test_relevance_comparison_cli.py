import json
import os
from pathlib import Path

import pytest

from scripts import compare_discovery_relevance
from scripts.compare_discovery_relevance import (
    _load_groups,
    _resolve_group_profile_maps,
)
from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore
from src.discovery.models import PaperCandidate
from src.discovery.relevance_comparison import fetch_openalex_corpus, fetch_synthetic_corpus
from tests.helpers.relevance_profiles import relevance_profile


KEYWORD_ID = "0123456789abcdef"
QUERY_ID = "fedcba9876543210"


def _sampling_profile() -> dict:
    return {
        "schema_version": "1.0",
        "subfield_union": ["S1"],
        "provider_sort": {"openalex": "relevance" + "_" + "score:desc"},
        "queries": ["aeolian"],
        "lanes": ["refresh"],
        "time_window": {"from": "", "to": ""},
        "budgets": [{
            "keyword_id": KEYWORD_ID,
            "provider": "openalex",
            "lane": "refresh",
            "query_id": QUERY_ID,
            "query": "aeolian",
            "target": 1,
        }],
    }


def test_comparison_rejects_fake_abc_copy(tmp_path: Path):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({
        "profiles": [{"keyword_zh": "风沙动力学", "profile": relevance_profile()}]
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="distinct groups"):
        _load_groups(path)


def test_comparison_has_one_shared_fetch_phase_for_all_replay_groups():
    assert hasattr(compare_discovery_relevance, "fetch_openalex_corpus")


def test_cli_requires_exactly_one_mode_before_reading_files(tmp_path: Path):
    missing_sampling = tmp_path / "missing-sampling.json"
    output_root = tmp_path / "corpus"

    with pytest.raises(SystemExit):
        compare_discovery_relevance.main([
            "--sampling-config", str(missing_sampling),
            "--output-root", str(output_root),
        ])
    with pytest.raises(SystemExit):
        compare_discovery_relevance.main([
            "--fetch", "--replay",
            "--sampling-config", str(missing_sampling),
            "--output-root", str(output_root),
        ])


def test_fetch_does_not_load_replay_inputs(tmp_path: Path, monkeypatch):
    sampling_path = tmp_path / "sampling.json"
    sampling_path.write_text(json.dumps(_sampling_profile()), encoding="utf-8")
    output_root = tmp_path / "corpus"
    calls = []

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Replay-only input loader was called during Fetch")

    monkeypatch.setattr(compare_discovery_relevance, "_load_groups", fail_if_called)
    monkeypatch.setattr(
        compare_discovery_relevance,
        "_resolve_group_profile_maps",
        fail_if_called,
    )
    monkeypatch.setattr(compare_discovery_relevance, "_known_dois", fail_if_called)

    def fake_fetch(**kwargs):
        calls.append(kwargs)
        return {"files": {"candidates": {"record_count": 0}}}

    monkeypatch.setattr(compare_discovery_relevance, "fetch_openalex_corpus", fake_fetch)

    assert compare_discovery_relevance.main([
        "--fetch",
        "--sampling-config", str(sampling_path),
        "--output-root", str(output_root),
        "--allow-network-fetch",
    ]) == 0
    assert len(calls) == 1


def test_cli_rejects_mode_specific_arguments_before_reading_files(tmp_path: Path):
    missing_sampling = tmp_path / "missing-sampling.json"
    output_root = tmp_path / "corpus"

    fetch_base = [
        "--fetch",
        "--sampling-config", str(missing_sampling),
        "--output-root", str(output_root),
        "--allow-network-fetch",
    ]
    for option, value in (
        ("--profiles", tmp_path / "profiles.json"),
        ("--notebook-root", tmp_path / "notebooks"),
        ("--known-dois", tmp_path / "known-dois.txt"),
    ):
        with pytest.raises(SystemExit):
            compare_discovery_relevance.main(fetch_base + [option, str(value)])

    replay_base = [
        "--replay",
        "--profiles", str(tmp_path / "profiles.json"),
        "--notebook-root", str(tmp_path / "notebooks"),
        "--sampling-config", str(missing_sampling),
        "--output-root", str(output_root),
        "--allow-network-fetch",
    ]
    with pytest.raises(SystemExit):
        compare_discovery_relevance.main(replay_base)


def test_replay_requires_profiles_and_notebook_before_reading_files(tmp_path: Path):
    with pytest.raises(SystemExit):
        compare_discovery_relevance.main([
            "--replay",
            "--sampling-config", str(tmp_path / "missing-sampling.json"),
            "--output-root", str(tmp_path / "corpus"),
        ])


def test_replay_cli_publishes_only_authoritative_run(tmp_path: Path, capsys):
    sampling = _sampling_profile()
    sampling_path = tmp_path / "sampling.json"
    sampling_path.write_text(json.dumps(sampling), encoding="utf-8")
    corpus_root = tmp_path / "corpus"

    fetch_synthetic_corpus(
        sampling_profile=sampling,
        output_root=corpus_root,
        provider_fetchers={
            "openalex": lambda **_kwargs: [
                PaperCandidate(title="aeolian sand saltation", doi="10.1/shared")
            ]
        },
    )

    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text(json.dumps({
        arm: [{
            "keyword_id": KEYWORD_ID,
            "profile": relevance_profile(object_term=f"sand {arm}"),
        }]
        for arm in "ABC"
    }), encoding="utf-8")

    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    temporary_link = corpus_root / "relevance_comparison.json.tmp"
    try:
        os.symlink(sentinel, temporary_link)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable on this platform")

    assert compare_discovery_relevance.main([
        "--replay",
        "--profiles", str(profiles_path),
        "--notebook-root", str(tmp_path / "notebooks"),
        "--sampling-config", str(sampling_path),
        "--output-root", str(corpus_root),
    ]) == 0

    run_dirs = [path for path in (corpus_root / "replay_runs").iterdir() if path.is_dir()]
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    assert all((run_dir / name).is_file() for name in ("manifest.json", "report.json", "COMMITTED"))
    legacy_output = corpus_root / "relevance_comparison.json"
    assert not legacy_output.exists()
    assert not legacy_output.is_symlink()
    assert temporary_link.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert f"Replay committed: {run_dir.absolute()}" in capsys.readouterr().out


def test_shared_fetch_calls_each_sampling_key_once_not_once_per_arm(tmp_path: Path):
    calls = []

    def fake_fetch(*, key, sampling_profile):
        calls.append((key["provider"], key["query_id"]))
        return [PaperCandidate(title="aeolian sand saltation", doi="10.1/shared")]

    sampling = {
        "schema_version": "1.0",
        "subfield_union": ["S1"], "provider_sort": {
            "openalex": "relevance" + "_" + "score:desc"},
        "queries": ["aeolian"], "lanes": ["refresh"], "time_window": {"from": "", "to": ""},
        "budgets": [{
            "keyword_id": KEYWORD_ID, "provider": "openalex", "lane": "refresh",
            "query_id": QUERY_ID, "query": "aeolian", "target": 1,
        }],
    }
    fetch_synthetic_corpus(
        sampling_profile=sampling,
        output_root=tmp_path,
        provider_fetchers={"openalex": fake_fetch},
    )
    assert calls == [("openalex", QUERY_ID)]
    manifest = json.loads((tmp_path / "corpus_manifest.json").read_text(encoding="utf-8"))
    assert manifest["budgets"][0]["unique_actual"] == 1


def test_actual_crossref_comparison_is_rejected_before_output(tmp_path: Path):
    sampling = {
        "schema_version": "1.0",
        "subfield_union": ["S1"], "provider_sort": {"crossref": "relevance"},
        "queries": ["q"], "lanes": ["refresh"], "time_window": {"from": "", "to": ""},
        "budgets": [{"keyword_id": KEYWORD_ID, "provider": "crossref",
                     "lane": "refresh", "query_id": QUERY_ID, "query": "q",
                     "target": 1, "order": "desc"}],
    }
    with pytest.raises(ValueError, match="actual_crossref_comparison_not_supported"):
        fetch_openalex_corpus(
            sampling_profile=sampling,
            output_root=tmp_path / "corpus",
            provider_fetchers={"openalex": lambda _request: None},
        )
    assert not (tmp_path / "corpus").exists()


def test_replay_profile_labels_resolve_to_corpus_keyword_ids(tmp_path: Path):
    notebook = KeywordNotebookStore(tmp_path / "notebooks")
    created = notebook.create_notebook("风沙动力学", enabled=False)
    groups = {
        arm: {"profile_map": {"风沙动力学": relevance_profile(object_term=f"sand {arm}")}}
        for arm in "ABC"
    }
    resolved = _resolve_group_profile_maps(groups, tmp_path / "notebooks")
    assert all(set(resolved[arm]) == {created["keyword_id"]} for arm in "ABC")
