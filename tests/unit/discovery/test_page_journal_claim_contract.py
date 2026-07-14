from pathlib import Path

import pytest

from src.discovery.keyword_notebook import keyword_id, query_identity
from src.discovery.models import PaperCandidate
from src.discovery.page_journal import INITIAL_CURSOR, PageJournalStore, request_signature


pytestmark = pytest.mark.unit
KEYWORD_ZH = "测试关键词"
KEYWORD_ID = keyword_id(KEYWORD_ZH)
QUERY_ID = query_identity("zh", KEYWORD_ZH)


def test_claim_candidate_rejects_uncommitted_page(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    path = store.write_page(store.make_page(
        page_id="p1",
        keyword_id=KEYWORD_ID,
        keyword_zh=KEYWORD_ZH,
        query_id=QUERY_ID,
        query=KEYWORD_ZH,
        query_language="zh",
        provider="openalex",
        lane="backfill",
        request_signature_value=request_signature(page_size=10),
        request_cursor=INITIAL_CURSOR,
        next_cursor="next",
        provider_exhausted=False,
        candidates=[PaperCandidate(title="T", doi="10.1234/page-state")],
        state="fetched",
    ))
    cid = store.read(path)["candidates"][0]["candidate_id"]

    claim = store.claim_candidate(path, candidate_id_value=cid, worker_id="w1", lease_seconds=60)

    assert not claim.claimed
    assert claim.reason == "page_not_claimable:fetched"
    assert store.read(path)["state"] == "fetched"
    assert store.read(path)["candidates"][0]["status"] == "pending"


def test_defer_candidate_releases_processing_claim(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    path = store.write_page(store.make_page(
        page_id="p1",
        keyword_id=KEYWORD_ID,
        keyword_zh=KEYWORD_ZH,
        query_id=QUERY_ID,
        query=KEYWORD_ZH,
        query_language="zh",
        provider="openalex",
        lane="backfill",
        request_signature_value=request_signature(page_size=10),
        request_cursor=INITIAL_CURSOR,
        next_cursor="next",
        provider_exhausted=False,
        candidates=[PaperCandidate(title="T", doi="10.1234/defer")],
        state="cursor_committed",
    ))
    cid = store.read(path)["candidates"][0]["candidate_id"]
    assert store.claim_candidate(path, candidate_id_value=cid, worker_id="w1", lease_seconds=60).claimed

    item = store.defer_candidate(
        path,
        candidate_id_value=cid,
        worker_id="w1",
        reason="formal_workspace_repair_required",
        drain_generation="drain-test",
    )

    assert item["status"] == "failed_retryable"
    assert item["claimed_by"] is None
    assert item["last_deferred_reason"] == "formal_workspace_repair_required"
    assert item["deferred_generation"] == "drain-test"
