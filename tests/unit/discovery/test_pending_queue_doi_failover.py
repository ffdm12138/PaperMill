from pathlib import Path

import pytest

from src.discovery.keyword_notebook import keyword_id, query_identity
from src.discovery.models import PaperCandidate
from src.discovery.page_journal import INITIAL_CURSOR, PageJournalStore, request_signature
from src.discovery.pending_queue import drain_pending_candidates


pytestmark = pytest.mark.unit
KEYWORD_ZH = "测试关键词"
KEYWORD_ID = keyword_id(KEYWORD_ZH)
QUERY_ID = query_identity("zh", KEYWORD_ZH)
PROFILE_HASH = "test-active-profile"


def _write_page(store: PageJournalStore, page_id: str, doi: str = "10.1234/same") -> Path:
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
        candidates=[PaperCandidate(title=f"T {page_id}", doi=doi)],
        state="cursor_committed", relevance_profile_hash=PROFILE_HASH,
    )
    page["candidates"][0]["relevance"]["state"] = "passed"
    return store.write_page(page)


def _drain(tmp_path: Path, store: PageJournalStore):
    return drain_pending_candidates(
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


def test_processing_primary_does_not_terminal_duplicate_secondary(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    path_a = _write_page(store, "p1")
    path_b = _write_page(store, "p2")
    cid_a = store.read(path_a)["candidates"][0]["candidate_id"]
    cid_b = store.read(path_b)["candidates"][0]["candidate_id"]

    assert store.claim_candidate(path_a, candidate_id_value=cid_a, worker_id="worker-a", lease_seconds=60, expected_profile_hash=PROFILE_HASH).claimed

    first = _drain(tmp_path, store)
    assert first.duplicate_observation == 0
    assert first.retryable_failures == 1
    item_b = store.read(path_b)["candidates"][0]
    assert item_b["candidate_id"] == cid_b
    assert item_b["status"] == "failed_retryable"
    assert item_b["last_deferred_reason"] == "doi_primary_processing"

    store.commit_candidate(path_a, candidate_id_value=cid_a, worker_id="worker-a", new_status="failed_terminal")
    second = _drain(tmp_path, store)
    assert second.emitted == 1
    assert store.read(path_b)["candidates"][0]["status"] == "emitted"


def test_staged_journal_without_workspace_does_not_terminal_duplicate_secondary(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    path_a = _write_page(store, "p1", doi="10.1234/missing-workspace")
    path_b = _write_page(store, "p2", doi="10.1234/missing-workspace")
    cid_a = store.read(path_a)["candidates"][0]["candidate_id"]

    assert store.claim_candidate(path_a, candidate_id_value=cid_a, worker_id="worker-a", lease_seconds=60, expected_profile_hash=PROFILE_HASH).claimed
    store.commit_candidate(
        path_a,
        candidate_id_value=cid_a,
        worker_id="worker-a",
        new_status="staged",
        updates={"staged_paper_number": "0000000000000999"},
    )

    report = _drain(tmp_path, store)
    assert report.duplicate_observation == 0
    assert report.emitted == 1
    item_b = store.read(path_b)["candidates"][0]
    assert item_b["status"] == "emitted"
