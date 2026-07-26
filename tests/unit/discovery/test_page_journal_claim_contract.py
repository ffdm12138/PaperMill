from pathlib import Path

import pytest

from src.discovery.contracts.notebook import keyword_id, query_identity
from src.discovery.models import PaperCandidate
from src.discovery.contracts.page_journal import INITIAL_CURSOR, request_signature
from src.discovery.stores.page_journal_store import PageJournalStoreV4 as PageJournalStore


pytestmark = pytest.mark.unit
KEYWORD_ZH = "测试关键词"
KEYWORD_ID = keyword_id(KEYWORD_ZH)
QUERY_ID = query_identity("zh", KEYWORD_ZH)


def test_claim_candidate_rejects_uncommitted_page(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    path = store.write_page(store.make_synthetic_page(
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
        relevance_profile_hash="test-hash",
    ))
    cid = store.read(path)["candidates"][0]["candidate_id"]

    claim = store.claim_candidate(path, candidate_id_value=cid, worker_id="w1", lease_seconds=60, expected_profile_hash="test-hash")

    assert not claim.claimed
    assert claim.reason == "page_not_claimable:fetched"
    assert store.read(path)["state"] == "fetched"
    assert store.read(path)["candidates"][0]["status"] == "pending"


def test_defer_candidate_releases_processing_claim(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    page = store.make_synthetic_page(
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
        relevance_profile_hash="test-hash",
    )
    # Candidate starts as profile_unbound; must be explicitly passed to be claimable.
    page["candidates"][0]["relevance"]["state"] = "passed"
    page["candidates"][0]["relevance"]["reason"] = "profile_match"
    path = store.write_page(page)
    cid = store.read(path)["candidates"][0]["candidate_id"]
    assert store.claim_candidate(path, candidate_id_value=cid, worker_id="w1", lease_seconds=60, expected_profile_hash="test-hash").claimed

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
