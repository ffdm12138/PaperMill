from pathlib import Path

from src.discovery.constants import INITIAL_CURSOR
from src.discovery.models import PaperCandidate
from src.discovery.page_journal import JournalDrainIndex, PageJournalStore, request_signature
from src.discovery.keyword_notebook import keyword_id, query_identity


def _page(store: PageJournalStore, page_id: str, count: int = 4) -> Path:
    page = store.make_page(
        page_id=page_id, keyword_id=keyword_id("关键词"), keyword_zh="关键词",
        query_id=query_identity("en", "keyword"),
        query="keyword", query_language="en", provider="crossref", lane="refresh",
        request_signature_value=request_signature(page_size=10), request_cursor=INITIAL_CURSOR,
        next_cursor=None, provider_exhausted=True,
        candidates=[PaperCandidate(title=f"P{i}", doi=f"10.9000/{page_id}-{i}") for i in range(count)],
        state="cursor_committed")
    return store.write_page(page)


def test_journal_drain_index_reads_pages_once_and_updates_in_memory(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    paths = [_page(store, f"p{i}") for i in range(3)]
    index = JournalDrainIndex.build(store)
    assert index.full_scans == 1
    assert index.pages_read == 3
    kid = keyword_id("关键词")
    assert index.pending_count([kid]) == 12
    ref = index.claimable([kid])[0]
    claim = store.claim_candidates_from_page(
        ref.page_path, worker_id="worker", lease_seconds=300, limit=1,
        candidate_ids=[ref.candidate_id])[0]
    index.update_candidate(ref.page_path, claim.payload)
    assert index.processing_by_doi[claim.doi] == claim.candidate_id
    assert index.pages_read == 3


def test_page_batch_claim_and_commit_use_one_mutation_each(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    path = _page(store, "batch", count=10)
    claims = store.claim_candidates_from_page(
        path, worker_id="worker", lease_seconds=300, limit=10)
    assert len(claims) == 10
    committed = store.commit_candidate_results(path, [{
        "candidate_id": claim.candidate_id, "new_status": "existing_duplicate",
        "updates": {"terminal_reason": "test"},
    } for claim in claims], worker_id="worker")
    assert len(committed) == 10
    assert store.read(path)["state"] == "drained"


def test_failed_retryable_returns_to_current_runtime_claimable_queue(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    path = _page(store, "retry", count=1)
    index = JournalDrainIndex.build(store)
    kid = keyword_id("关键词")
    ref = index.claimable([kid])[0]
    claim = store.claim_candidates_from_page(
        path, worker_id="worker", lease_seconds=300, limit=1,
        candidate_ids=[ref.candidate_id])[0]
    index.update_candidate(path, claim.payload)
    assert index.pending_count([kid]) == 0

    committed = store.commit_candidate_results(path, [{
        "candidate_id": claim.candidate_id,
        "new_status": "failed_retryable",
        "updates": {"next_attempt_at": None, "last_error": "synthetic"},
    }], worker_id="worker")[0]
    index.update_candidate(path, committed)

    assert store.read(path)["candidates"][0]["status"] == "failed_retryable"
    assert index.pending_count([kid]) == 1
    assert [item.candidate_id for item in index.claimable([kid])] == [claim.candidate_id]
