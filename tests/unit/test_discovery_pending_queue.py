from __future__ import annotations

from pathlib import Path

import pytest

from src.discovery.keyword_notebook import keyword_id, query_identity
from src.discovery.models import PaperCandidate
from src.discovery.page_journal import INITIAL_CURSOR, PageJournalStore, request_signature
from src.discovery.pending_queue import drain_pending_candidates
from src.discovery.resolve_crossref import ResolvedDoiMatch


pytestmark = pytest.mark.unit
KEYWORD_ZH = "测试关键词"
KEYWORD_ID = keyword_id(KEYWORD_ZH)
QUERY_ID = query_identity("zh", KEYWORD_ZH)


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
        state="cursor_committed",
    )
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
        state="cursor_committed",
    )
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
    )
    assert second.duplicate_observation == 1
    statuses = [item["status"] for ref in store.list_pages([KEYWORD_ID]) for item in store.read(ref.path)["candidates"]]
    assert sorted(statuses) == ["duplicate_observation", "emitted"]
