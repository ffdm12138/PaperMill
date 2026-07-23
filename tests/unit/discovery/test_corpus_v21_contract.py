from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from src.discovery.models import PaperCandidate
from src.discovery.providers.provider_models import DiscoveryPage
from src.discovery.providers.provider_request_evidence import (
    ActualRequestEvidence,
    build_safe_signature,
    safe_response_hash,
    scan_safe_signature_for_credentials,
)
from src.discovery.relevance_comparison import (
    CORPUS_SCHEMA_VERSION,
    SamplingBudget,
    SamplingProfile,
    collect_budget_sample,
    fetch_openalex_corpus,
    fetch_synthetic_corpus,
    replay_frozen_corpus,
    verify_corpus,
)
from src.discovery.search_openalex import parse_openalex_work
from tests.helpers.relevance_profiles import relevance_profile


KEYWORD_ID = "0123456789abcdef"
QUERY_ID = "fedcba9876543210"


def _profile(*, target: int = 2) -> dict:
    return {
        "schema_version": "1.0",
        "subfield_union": ["S1"],
        "provider_sort": {"openalex": "relevance" + "_" + "score:desc"},
        "queries": ["aeolian"],
        "lanes": ["refresh"],
        "time_window": {"from": "", "to": ""},
        "budgets": [{
            "keyword_id": KEYWORD_ID, "provider": "openalex", "lane": "refresh",
            "query_id": QUERY_ID, "query": "aeolian", "target": target,
        }],
    }


def _page_fetcher(request):
    if request.cursor == "*":
        works = [
            {
                "id": "https://openalex.org/W-A",
                "display_name": "A",
                "doi": "https://doi.org/10.1000/a",
                "publication_year": 2024,
                "authorships": [],
                "primary_location": {"source": {"display_name": "Test"}},
                "open_access": {"is_oa": False},
                "cited_by_count": 0,
            },
            {
                "id": "https://openalex.org/W-A",
                "display_name": "A",
                "doi": "https://doi.org/10.1000/a",
                "publication_year": 2024,
                "authorships": [],
                "primary_location": {"source": {"display_name": "Test"}},
                "open_access": {"is_oa": False},
                "cited_by_count": 0,
            },
        ]
        next_cursor = "page-2"
        exhausted = False
    else:
        works = [{
            "id": "https://openalex.org/W-B",
            "display_name": "B",
            "doi": "https://doi.org/10.1000/b",
            "publication_year": 2024,
            "authorships": [],
            "primary_location": {"source": {"display_name": "Test"}},
            "open_access": {"is_oa": False},
            "cited_by_count": 0,
        }]
        next_cursor = None
        exhausted = True
    works = works[:request.page_size]
    candidates = [parse_openalex_work(work, query=request.query) for work in works]
    body = json.dumps({
        "cursor": request.cursor,
        "meta": {"next_cursor": next_cursor},
        "results": works,
    }, sort_keys=True).encode("utf-8")
    if request.request_observer is not None:
        request.request_observer(ActualRequestEvidence(
            safe_signature=build_safe_signature(
                provider="openalex", query=request.query,
                lane=request.lane, sort="relevance" + "_" + "score:desc",
                filter="topics.subfield.id:S1", topic_filter="topics.subfield.id:S1",
                page_size=request.page_size,
                time_window={"from": "", "to": ""},
                pagination_schema_version="2.0",
            ),
            cursor_in=request.cursor, cursor_out=next_cursor,
            response_hash=safe_response_hash(body),
            observation_count=len(candidates), response_bytes=body,
            request_timestamp="2026-07-18T00:00:00+00:00",
        ))
    return DiscoveryPage(
        provider="openalex", keyword_zh=KEYWORD_ID, query=request.query,
        query_id=request.budget.query_id, lane=request.lane,
        candidates=candidates, request_cursor=request.cursor,
        next_cursor=next_cursor, page_size=request.page_size,
        returned_count=len(candidates), exhausted=exhausted,
    )


def test_sampling_target_counts_unique_papers_across_duplicate_pages():
    budget = SamplingBudget(
        keyword_id=KEYWORD_ID, provider="openalex", lane="refresh", query_id=QUERY_ID,
        query="aeolian", target=2,
    )
    result = collect_budget_sample(budget, fetch_page=_page_fetcher)
    assert result.raw_actual == 3
    assert result.unique_actual == 2
    assert result.duplicate_observation_count == 1
    assert len(result.request_evidence) == 2
    assert result.final_cursor == "page-2"


def test_actual_corpus_is_self_verifying_and_response_blobs_are_replayable(tmp_path: Path):
    root = tmp_path / "corpus"
    manifest = fetch_openalex_corpus(
        sampling_profile=_profile(), output_root=root,
        provider_fetchers={"openalex": _page_fetcher},
    )
    assert manifest["schema_version"] == CORPUS_SCHEMA_VERSION
    assert verify_corpus(root)["corpus_hash"] == manifest["corpus_hash"]
    blobs = list((root / "blobs" / "provider_response").glob("*.json"))
    assert len(blobs) == 2


def test_actual_corpus_without_evidence_leaves_no_output(tmp_path: Path):
    root = tmp_path / "corpus"

    def no_evidence_fetch(*, key, sampling_profile):
        return [PaperCandidate(title="A", doi="10.1000/a")]

    with pytest.raises(TypeError):
        fetch_openalex_corpus(
            sampling_profile=_profile(target=1), output_root=root,
            provider_fetchers={"openalex": no_evidence_fetch},
        )
    assert not root.exists()


def test_actual_candidate_and_raw_work_tamper_cannot_be_resigned(tmp_path: Path):
    root = tmp_path / "corpus"
    fetch_openalex_corpus(
        sampling_profile=_profile(target=1), output_root=root,
        provider_fetchers={"openalex": _page_fetcher},
    )
    manifest_path = root / "corpus_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    candidate_path = root / Path(manifest["files"]["candidates"]["path"])
    raw_path = root / Path(manifest["files"]["raw_openalex_works"]["path"])
    record = json.loads(candidate_path.read_text(encoding="utf-8"))
    record["candidate"]["title"] = "forged title"
    record["candidate"]["doi"] = "10.1000/forged"
    record["candidate"]["raw"] = {"id": "https://openalex.org/W-forged"}
    record["provider_observation"] = dict(record["candidate"]["raw"])
    from src.discovery.relevance_comparison import _observation_id, _paper_identity
    record["paper_identity"] = _paper_identity(record["candidate"], "openalex")
    sequence = int(record["observation_id"].split("-", 1)[0])
    record["observation_id"] = (
        f"{sequence}-" + _observation_id(
            record["budget_id"], sequence, record["provider_rank"], record["paper_identity"]
        )
    )
    record["raw_candidate_sha256"] = hashlib.sha256(
        (json.dumps(record["candidate"], ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    candidate_payload = (
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    candidate_path.write_bytes(candidate_payload)
    raw_record = json.loads(raw_path.read_text(encoding="utf-8"))
    raw_record["observation_id"] = record["observation_id"]
    raw_record["doi"] = record["candidate"]["doi"]
    raw_record["work"] = dict(record["candidate"]["raw"])
    raw_payload = (
        json.dumps(raw_record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    raw_path.write_bytes(raw_payload)
    manifest["files"]["candidates"].update({
        "sha256": hashlib.sha256(candidate_payload).hexdigest(),
        "size": len(candidate_payload),
    })
    manifest["files"]["raw_openalex_works"].update({
        "sha256": hashlib.sha256(raw_payload).hexdigest(),
        "size": len(raw_payload),
    })
    unsigned = {key: value for key, value in manifest.items() if key != "corpus_hash"}
    manifest["corpus_hash"] = hashlib.sha256(
        (json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="derived from response blob|semantic response drift"):
        verify_corpus(root)


def test_actual_provider_rank_is_global_across_pages(tmp_path: Path):
    root = tmp_path / "corpus"
    fetch_openalex_corpus(
        sampling_profile=_profile(target=2), output_root=root,
        provider_fetchers={"openalex": _page_fetcher},
    )
    manifest = json.loads((root / "corpus_manifest.json").read_text(encoding="utf-8"))
    records = [
        json.loads(line)
        for line in (root / Path(manifest["files"]["candidates"]["path"])).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    records.sort(key=lambda item: (int(item["observation_id"].split("-", 1)[0]), item["provider_rank"]))
    assert [item["provider_rank"] for item in records] == [1, 2, 3]


def test_actual_crossref_comparison_is_rejected_before_output(tmp_path: Path):
    profile = _profile()
    profile["provider_sort"] = {"crossref": "relevance"}
    profile["budgets"][0].update({
        "provider": "crossref", "order": "desc",
    })
    root = tmp_path / "corpus"
    with pytest.raises(ValueError, match="actual_crossref_comparison_not_supported"):
        fetch_openalex_corpus(
            sampling_profile=profile, output_root=root,
            provider_fetchers={"openalex": _page_fetcher},
        )
    assert not root.exists()


def test_sampling_profile_closes_lanes_and_canonicalizes_subfields():
    profile = _profile()
    profile["subfield_union"] = ["S1", "https://openalex.org/subfields/S1"]
    with pytest.raises(ValueError, match="duplicate IDs"):
        SamplingProfile.parse_and_validate(profile)
    profile = _profile()
    profile["budgets"][0]["lane"] = "backfill"
    with pytest.raises(ValueError, match="declared in profile lanes"):
        SamplingProfile.parse_and_validate(profile)
    profile = _profile()
    profile["provider_sort"] = {"openalex": {"backfill": "relevance" + "_score:desc"}}
    with pytest.raises(ValueError, match="declared in lanes"):
        SamplingProfile.parse_and_validate(profile)


def test_sampling_page_contract_rejects_wrong_size_and_count():
    budget = SamplingBudget(
        keyword_id=KEYWORD_ID, provider="openalex", lane="refresh",
        query_id=QUERY_ID, query="aeolian", target=1,
    )

    def bad_page(request):
        candidate = PaperCandidate(title="A", doi="10.1000/a")
        return DiscoveryPage(
            provider="openalex", keyword_zh=KEYWORD_ID, query=request.query,
            query_id=QUERY_ID, lane=request.lane, candidates=[candidate],
            request_cursor=request.cursor, next_cursor=None,
            page_size=request.page_size + 1, returned_count=99, exhausted=True,
        )

    with pytest.raises(ValueError, match="page_size"):
        collect_budget_sample(budget, fetch_page=bad_page)


def test_corpus_publish_rejects_existing_blob_symlink_before_writing(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "corpus"
    root.mkdir()
    try:
        os.symlink(outside, root / "blobs", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable on this platform")
    with pytest.raises(ValueError, match="symlink/reparse"):
        fetch_synthetic_corpus(
            sampling_profile=_profile(target=1), output_root=root,
            provider_fetchers={"openalex": lambda **_kwargs: [PaperCandidate(title="A")]},
        )
    assert list(outside.iterdir()) == []


def test_synthetic_fixture_rejects_unique_target_overage(tmp_path: Path):
    root = tmp_path / "corpus"
    with pytest.raises(ValueError, match="synthetic fixture exceeds target"):
        fetch_synthetic_corpus(
            sampling_profile=_profile(target=1), output_root=root,
            provider_fetchers={"openalex": lambda **_kwargs: [
                PaperCandidate(title="A", doi="10.1000/a"),
                PaperCandidate(title="B", doi="10.1000/b"),
            ]},
        )
    assert not root.exists()


def test_credential_scanner_handles_casing_and_nested_sequences():
    found = scan_safe_signature_for_credentials({
        "outer": [{"apiKey": "x"}, ("safe", {"Authorization": "Bearer x"})],
    })
    assert set(found) == {"apiKey", "Authorization"}


def test_evidence_wire_contract_is_strict():
    body = b'{"ok":true}'
    evidence = ActualRequestEvidence(
        safe_signature=build_safe_signature(
            provider="openalex", query="q", lane="refresh", sort="",
            filter="", topic_filter="", page_size=1,
            time_window={"from": "", "to": ""}, pagination_schema_version="2.0",
        ),
        cursor_in="*", cursor_out="", response_hash=safe_response_hash(body),
        request_timestamp="2026-07-18T00:00:00+00:00", observation_count=0,
        budget_id="kid:openalex:refresh:qid", request_sequence=1,
        response_blob_path="blobs/provider_response/" + safe_response_hash(body) + ".json",
        response_bytes=body,
    )
    payload = evidence.to_dict()
    assert ActualRequestEvidence.from_dict(payload).to_dict() == payload
    for key, value in (("schema_version", "9.9"), ("request_timestamp", "2026-07-18"), ("response_hash", "abc")):
        tampered = dict(payload, **{key: value})
        with pytest.raises(ValueError):
            ActualRequestEvidence.from_dict(tampered)


def test_semantic_candidate_tamper_is_not_repaired_by_resigning_file_hashes(tmp_path: Path):
    root = tmp_path / "corpus"
    fetch_synthetic_corpus(
        sampling_profile=_profile(target=1), output_root=root,
        provider_fetchers={"openalex": lambda **_kwargs: [PaperCandidate(title="A", doi="10.1000/a")]},
    )
    manifest_path = root / "corpus_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    candidate_path = root / Path(manifest["files"]["candidates"]["path"])
    record = json.loads(candidate_path.read_text(encoding="utf-8"))
    record["observation_id"] = "1-" + hashlib.sha256(b"forged").hexdigest()
    payload = (json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    candidate_path.write_bytes(payload)
    manifest["files"]["candidates"].update({
        "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload),
    })
    unsigned = dict(manifest)
    unsigned.pop("corpus_hash")
    manifest["corpus_hash"] = hashlib.sha256(
        (json.dumps(unsigned, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="observation_id"):
        verify_corpus(root)


def test_manifest_path_escape_is_rejected_even_after_hash_recalculation(tmp_path: Path):
    root = tmp_path / "corpus"
    fetch_synthetic_corpus(
        sampling_profile=_profile(target=1), output_root=root,
        provider_fetchers={"openalex": lambda **_kwargs: [PaperCandidate(title="A", doi="10.1000/a")]},
    )
    path = root / "corpus_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"]["candidates"]["path"] = "../outside.jsonl"
    unsigned = dict(manifest)
    unsigned.pop("corpus_hash")
    manifest["corpus_hash"] = hashlib.sha256(
        (json.dumps(unsigned, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="POSIX|escapes"):
        verify_corpus(root)


def test_replay_run_identity_is_canonical_and_idempotent(tmp_path: Path):
    root = tmp_path / "corpus"
    fetch_synthetic_corpus(
        sampling_profile=_profile(target=1), output_root=root,
        provider_fetchers={"openalex": lambda **_kwargs: [
            PaperCandidate(title="A", doi="10.1000/a")
        ]},
    )
    groups = {
        arm: {KEYWORD_ID: relevance_profile(object_term=f"wind-blown sand {arm}")}
        for arm in "ABC"
    }
    first = replay_frozen_corpus(corpus_root=root, groups=groups)
    run_dir = root / "replay_runs" / first["run_id"]
    before = {
        name: (run_dir / name).read_bytes()
        for name in ("manifest.json", "report.json", "COMMITTED")
    }
    second = replay_frozen_corpus(corpus_root=root, groups=groups)
    assert second["run_id"] == first["run_id"]
    assert {
        name: (run_dir / name).read_bytes()
        for name in ("manifest.json", "report.json", "COMMITTED")
    } == before


def test_replay_refuses_existing_report_tamper_with_retained_hash(tmp_path: Path):
    root = tmp_path / "corpus"
    fetch_synthetic_corpus(
        sampling_profile=_profile(target=1), output_root=root,
        provider_fetchers={"openalex": lambda **_kwargs: [
            PaperCandidate(title="A", doi="10.1000/a")
        ]},
    )
    groups = {
        arm: {KEYWORD_ID: relevance_profile(object_term=f"sand {arm}")}
        for arm in "ABC"
    }
    report = replay_frozen_corpus(corpus_root=root, groups=groups)
    report_path = root / "replay_runs" / report["run_id"] / "report.json"
    tampered = json.loads(report_path.read_text(encoding="utf-8"))
    tampered["groups"]["A"]["candidate_count"] = 999
    report_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="report|result_hash"):
        replay_frozen_corpus(corpus_root=root, groups=groups)


def test_replay_refuses_manifest_created_at_tamper(tmp_path: Path):
    root = tmp_path / "corpus"
    fetch_synthetic_corpus(
        sampling_profile=_profile(target=1), output_root=root,
        provider_fetchers={"openalex": lambda **_kwargs: [
            PaperCandidate(title="A", doi="10.1000/a")
        ]},
    )
    groups = {
        arm: {KEYWORD_ID: relevance_profile(object_term=f"sand {arm}")}
        for arm in "ABC"
    }
    report = replay_frozen_corpus(corpus_root=root, groups=groups)
    manifest_path = root / "replay_runs" / report["run_id"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["created_at"] = "2020-01-01T00:00:00+00:00"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest_hash"):
        replay_frozen_corpus(corpus_root=root, groups=groups)
