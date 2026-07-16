from __future__ import annotations

from pathlib import Path

import pytest

from src.discovery.keyword_notebook import keyword_id, query_identity
from src.discovery.models import PaperCandidate
from src.discovery.page_journal import INITIAL_CURSOR, PageJournalStore, request_signature
from src.discovery.pending_queue import (
    _inspect_emitted_primary_export_cached,
    drain_pending_candidates,
    export_candidate_once,
)
from src.discovery.page_journal import JournalDrainIndex
from src.discovery.resolve_crossref import ResolvedDoiMatch


pytestmark = pytest.mark.unit
KEYWORD_ZH = "测试关键词"
KEYWORD_ID = keyword_id(KEYWORD_ZH)
QUERY_ID = query_identity("zh", KEYWORD_ZH)
PROFILE_HASH = "test-active-profile"


def _write_page(store: PageJournalStore, page_id: str, doi: str = "10.1234/item") -> Path:
    page = store.make_page(
        page_id=page_id,
        keyword_id=KEYWORD_ID,
        keyword_zh=KEYWORD_ZH,
        query_id=QUERY_ID,
        query=KEYWORD_ZH,
        query_language="zh",
        provider="openalex",
        lane="refresh",
        request_signature_value=request_signature(page_size=10),
        request_cursor=INITIAL_CURSOR,
        next_cursor=None,
        provider_exhausted=True,
        candidates=[PaperCandidate(title=f"title {page_id}", doi=doi, source="openalex")],
        state="cursor_committed", relevance_profile_hash=PROFILE_HASH,
    )
    page["candidates"][0]["relevance"]["state"] = "passed"
    return store.write_page(page)


def _write_no_doi_page(store: PageJournalStore, page_id: str, title: str = "same unresolved title") -> Path:
    page = store.make_page(
        page_id=page_id,
        keyword_id=KEYWORD_ID,
        keyword_zh=KEYWORD_ZH,
        query_id=QUERY_ID,
        query=KEYWORD_ZH,
        query_language="zh",
        provider="openalex",
        lane="refresh",
        request_signature_value=request_signature(page_size=10),
        request_cursor=INITIAL_CURSOR,
        next_cursor=None,
        provider_exhausted=True,
        candidates=[PaperCandidate(title=title, doi="", source="openalex")],
        state="cursor_committed", relevance_profile_hash=PROFILE_HASH,
    )
    page["candidates"][0]["relevance"]["state"] = "passed"
    return store.write_page(page)


def test_candidate_claim_lease_renewal_and_owner_commit(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    path = _write_page(store, "p1")
    cid = store.read(path)["candidates"][0]["candidate_id"]

    claim = store.claim_candidate(path, candidate_id_value=cid, worker_id="worker-a", lease_seconds=60)
    assert claim.claimed
    assert not store.claim_candidate(path, candidate_id_value=cid, worker_id="worker-b", lease_seconds=60).claimed
    assert store.renew_candidate_lease(path, candidate_id_value=cid, worker_id="worker-a", lease_seconds=120)
    assert not store.renew_candidate_lease(path, candidate_id_value=cid, worker_id="worker-b", lease_seconds=120)
    with pytest.raises(Exception):
        store.commit_candidate(path, candidate_id_value=cid, worker_id="worker-b", new_status="emitted")
    store.commit_candidate(path, candidate_id_value=cid, worker_id="worker-a", new_status="emitted")
    assert store.read(path)["candidates"][0]["status"] == "emitted"


def test_expired_processing_lease_can_be_reclaimed(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    path = _write_page(store, "p1")
    cid = store.read(path)["candidates"][0]["candidate_id"]
    assert store.claim_candidate(path, candidate_id_value=cid, worker_id="worker-a", lease_seconds=-1).claimed
    claim = store.claim_candidate(path, candidate_id_value=cid, worker_id="worker-b", lease_seconds=60)
    assert claim.claimed
    assert claim.candidate["claimed_by"] == "worker-b"


def test_doi_duplicate_observation_across_pages(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    _write_page(store, "p1", doi="10.1234/same")
    _write_page(store, "p2", doi="10.1234/same")

    report = drain_pending_candidates(
        journal=store,
        keyword_ids=[KEYWORD_ID],
        candidate_budget=10,
        stage_to_paper_raw=False,
        apply=False,
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        locks_dir=tmp_path / "locks",
        exports_dir=tmp_path / "exports",
        worker_id="worker",
        active_profile_hashes={KEYWORD_ID: PROFILE_HASH},
    )
    assert report.emitted == 1
    assert report.duplicate_observation == 1
    statuses = [item["status"] for ref in store.list_pages([KEYWORD_ID]) for item in store.read(ref.path)["candidates"]]
    assert sorted(statuses) == ["duplicate_observation", "emitted"]


def test_resolved_doi_is_persisted_and_deduped_across_runs(tmp_path: Path, monkeypatch):
    store = PageJournalStore(tmp_path / "pages")
    _write_no_doi_page(store, "p1")
    _write_no_doi_page(store, "p2")

    def fake_resolve(title, year=None, domain_id=None):
        return ResolvedDoiMatch(
            doi="10.1234/resolved",
            provider="crossref",
            confidence=0.93,
            matched_title=title,
            raw_record={"DOI": "10.1234/resolved", "title": [title]},
        )

    monkeypatch.setattr("src.discovery.pending_queue.resolve_doi_match_by_title", fake_resolve)
    first = drain_pending_candidates(
        journal=store,
        keyword_ids=[KEYWORD_ID],
        candidate_budget=1,
        stage_to_paper_raw=False,
        apply=False,
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        locks_dir=tmp_path / "locks",
        exports_dir=tmp_path / "exports",
        worker_id="worker",
        active_profile_hashes={KEYWORD_ID: PROFILE_HASH},
    )
    assert first.emitted == 1
    persisted = [
        item["candidate"]
        for ref in store.list_pages([KEYWORD_ID])
        for item in store.read(ref.path)["candidates"]
        if item["status"] == "emitted"
    ][0]
    assert persisted["doi"] == "10.1234/resolved"
    assert persisted["doi_resolution"]["raw_record"]["DOI"] == "10.1234/resolved"

    second = drain_pending_candidates(
        journal=store,
        keyword_ids=[KEYWORD_ID],
        candidate_budget=10,
        stage_to_paper_raw=False,
        apply=False,
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        locks_dir=tmp_path / "locks",
        exports_dir=tmp_path / "exports",
        worker_id="worker",
        active_profile_hashes={KEYWORD_ID: PROFILE_HASH},
    )
    assert second.duplicate_observation == 1
    statuses = [item["status"] for ref in store.list_pages([KEYWORD_ID]) for item in store.read(ref.path)["candidates"]]
    assert sorted(statuses) == ["duplicate_observation", "emitted"]


def test_emitted_export_validation_cache_is_artifact_fingerprint_bound(
    tmp_path: Path, monkeypatch,
):
    import src.discovery.pending_queue as pending_queue

    doi = "10.1234/cached-export"
    record = {
        "candidate_id": "candidate-cache",
        "page_id": "page-cache",
        "keyword_id": KEYWORD_ID,
        "provider": "openalex",
        "candidate": PaperCandidate(title="Cached", doi=doi).to_dict(),
    }
    exported = export_candidate_once(tmp_path / "exports", record)
    item = {**record, **exported}
    index = JournalDrainIndex.build(
        PageJournalStore(tmp_path / "pages"),
        active_profile_hashes={KEYWORD_ID: PROFILE_HASH},
    )
    original = pending_queue.inspect_emitted_primary_export
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(pending_queue, "inspect_emitted_primary_export", counted)
    assert _inspect_emitted_primary_export_cached(
        index, item, doi, exports_dir=tmp_path / "exports") == (True, "")
    assert _inspect_emitted_primary_export_cached(
        index, item, doi, exports_dir=tmp_path / "exports") == (True, "")
    assert calls == 1

    jsonl = Path(exported["export_path"])
    jsonl.write_bytes(jsonl.read_bytes() + b" ")
    valid, reason = _inspect_emitted_primary_export_cached(
        index, item, doi, exports_dir=tmp_path / "exports")
    assert not valid
    assert reason == "artifact size mismatch"
    assert calls == 2
