from pathlib import Path

import pytest

from src.discovery.constants import INITIAL_CURSOR
from src.discovery.models import PaperCandidate
from src.discovery.contracts.page_journal import (
    JournalCorruptError,
    page_is_drain_visible,
    request_signature,
    select_stable_emitted_primary,
    validate_journal_drain_index,
)
from src.discovery.stores.journal_drain_index import JournalDrainIndex
from src.discovery.stores.page_journal_store import PageJournalStoreV4 as PageJournalStore
from src.discovery.contracts.notebook import keyword_id, query_identity

PROFILE_HASH = "test-active-profile"


def _page(store: PageJournalStore, page_id: str, count: int = 4) -> Path:
    page = store.make_synthetic_page(
        page_id=page_id, keyword_id=keyword_id("关键词"), keyword_zh="关键词",
        query_id=query_identity("en", "keyword"),
        query="keyword", query_language="en", provider="crossref", lane="refresh",
        request_signature_value=request_signature(page_size=10), request_cursor=INITIAL_CURSOR,
        next_cursor=None, provider_exhausted=True,
        candidates=[PaperCandidate(title=f"P{i}", doi=f"10.9000/{page_id}-{i}") for i in range(count)],
        state="cursor_committed", relevance_profile_hash=PROFILE_HASH)
    for candidate in page["candidates"]:
        candidate["relevance"]["state"] = "passed"
    return store.write_page(page)


def test_journal_drain_index_reads_pages_once_and_updates_in_memory(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    paths = [_page(store, f"p{i}") for i in range(3)]
    index = JournalDrainIndex.build(
        store, active_profile_hashes={keyword_id("关键词"): PROFILE_HASH})
    assert index.full_scans == 1
    assert index.pages_read == 3
    kid = keyword_id("关键词")
    assert index.pending_count([kid]) == 12
    ref = index.claimable([kid])[0]
    claim = store.claim_candidates_from_page(
        ref.page_path, worker_id="worker", lease_seconds=300, limit=1,
        candidate_ids=[ref.candidate_id], expected_profile_hash=PROFILE_HASH)[0]
    index.update_candidate(ref.page_path, claim.payload)
    assert index.get_processing_owner(claim.doi) == claim.candidate_id
    assert index.pages_read == 3


def test_page_batch_claim_and_commit_use_one_mutation_each(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    path = _page(store, "batch", count=10)
    claims = store.claim_candidates_from_page(
        path, worker_id="worker", lease_seconds=300, limit=10,
        expected_profile_hash=PROFILE_HASH)
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
    index = JournalDrainIndex.build(
        store, active_profile_hashes={keyword_id("关键词"): PROFILE_HASH})
    kid = keyword_id("关键词")
    ref = index.claimable([kid])[0]
    claim = store.claim_candidates_from_page(
        path, worker_id="worker", lease_seconds=300, limit=1,
        candidate_ids=[ref.candidate_id], expected_profile_hash=PROFILE_HASH)[0]
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


# ── Phase 1.1: Stable emitted-primary selection ───────────────────────


def test_emitted_primary_select_stable_by_candidate_id():
    """Lower candidate_id always wins regardless of argument order."""
    a = page_journal_module.EmittedPrimaryRef(
        "aaa", Path("a.json"), {"status": "emitted"})
    b = page_journal_module.EmittedPrimaryRef(
        "bbb", Path("b.json"), {"status": "emitted"})

    assert select_stable_emitted_primary(None, a) is a
    assert select_stable_emitted_primary(a, b) is a  # aaa < bbb
    assert select_stable_emitted_primary(b, a) is a  # order doesn't matter


def test_build_and_add_page_agree_on_emitted_owner(tmp_path: Path):
    """Cold build and incremental add_page must pick the same DOI owner."""
    store = PageJournalStore(tmp_path / "pages")
    kid = keyword_id("测试")
    sig = request_signature(page_size=10)

    # Two pages with the same DOI but different candidate_ids.
    p1 = store.make_synthetic_page(
        page_id="p1", keyword_id=kid, keyword_zh="测试",
        query_id=query_identity("en", "test"), query="test",
        query_language="en", provider="crossref", lane="refresh",
        request_signature_value=sig, request_cursor=INITIAL_CURSOR,
        next_cursor=None, provider_exhausted=True,
        candidates=[PaperCandidate(title="T", doi="10.1000/shared")],
        state="fetched", relevance_profile_hash=PROFILE_HASH)
    p2 = store.make_synthetic_page(
        page_id="p2", keyword_id=kid, keyword_zh="测试",
        query_id=query_identity("en", "test"), query="test",
        query_language="en", provider="crossref", lane="refresh",
        request_signature_value=sig, request_cursor=INITIAL_CURSOR,
        next_cursor=None, provider_exhausted=True,
        candidates=[PaperCandidate(title="T2", doi="10.1000/shared")],
        state="fetched", relevance_profile_hash=PROFILE_HASH)

    # Finalize relevance and mark both candidates as emitted.
    for p_data in (p1, p2):
        cid = p_data["candidates"][0]["candidate_id"]
        p_data["candidates"][0]["relevance"]["state"] = "passed"
        p_data["candidates"][0]["status"] = "emitted"
        p_data["state"] = "cursor_committed"

    path1 = store.write_page(p1)
    path2 = store.write_page(p2)

    bindings = {kid: PROFILE_HASH}

    # Cold build.
    cold = JournalDrainIndex.build(store, active_profile_hashes=bindings)
    cold_owner = cold.get_emitted_primary("10.1000/shared")

    # Incremental: add p1 then p2.
    inc1 = JournalDrainIndex.build(
        PageJournalStore(tmp_path / "empty_pages"),
        active_profile_hashes=bindings)
    inc1.add_page(path1, store.read(path1))
    inc1.add_page(path2, store.read(path2))
    inc_owner = inc1.get_emitted_primary("10.1000/shared")

    # Incremental reverse order.
    inc2 = JournalDrainIndex.build(
        PageJournalStore(tmp_path / "empty_pages2"),
        active_profile_hashes=bindings)
    inc2.add_page(path2, store.read(path2))
    inc2.add_page(path1, store.read(path1))
    inc2_owner = inc2.get_emitted_primary("10.1000/shared")

    assert cold_owner is not None
    assert cold_owner.candidate_id == inc_owner.candidate_id == inc2_owner.candidate_id


# ── Phase 1.2: Page-state gate ────────────────────────────────────────


def test_fetched_page_candidates_not_claimable(tmp_path: Path):
    """Candidates in a fetched page must not enter the claimable queue."""
    store = PageJournalStore(tmp_path / "pages")
    kid = keyword_id("测试")
    page_data = store.make_synthetic_page(
        page_id="pg", keyword_id=kid, keyword_zh="测试",
        query_id=query_identity("en", "test"), query="test",
        query_language="en", provider="crossref", lane="refresh",
        request_signature_value=request_signature(page_size=10),
        request_cursor=INITIAL_CURSOR, next_cursor=None,
        provider_exhausted=True,
        candidates=[PaperCandidate(title="T", doi="10.1000/test")],
        state="fetched", relevance_profile_hash=PROFILE_HASH)
    # Set relevance to passed but keep page as fetched.
    for c in page_data["candidates"]:
        c["relevance"]["state"] = "passed"
    page = store.write_page(page_data)

    # Cold build from disk — state is fetched, so no claimable.
    index = JournalDrainIndex.build(
        store, active_profile_hashes={kid: PROFILE_HASH})
    assert index.pending_count([kid]) == 0

    # add_page also respects the gate.
    index2 = JournalDrainIndex.build(
        PageJournalStore(tmp_path / "empty"), active_profile_hashes={kid: PROFILE_HASH})
    index2.add_page(page, page_data)
    assert index2.pending_count([kid]) == 0


def test_cursor_committed_page_candidates_are_claimable(tmp_path: Path):
    """Candidates in a cursor_committed page enter the claimable queue."""
    store = PageJournalStore(tmp_path / "pages")
    kid = keyword_id("测试")
    page = store.write_page(store.make_synthetic_page(
        page_id="pg", keyword_id=kid, keyword_zh="测试",
        query_id=query_identity("en", "test"), query="test",
        query_language="en", provider="crossref", lane="refresh",
        request_signature_value=request_signature(page_size=10),
        request_cursor=INITIAL_CURSOR, next_cursor=None,
        provider_exhausted=True,
        candidates=[PaperCandidate(title="T", doi="10.1000/test")],
        state="fetched", relevance_profile_hash=PROFILE_HASH))
    # Finalize relevance (profile_unbound → passed) then commit cursor.
    page_data = store.read(page)
    cid = page_data["candidates"][0]["candidate_id"]
    store.finalize_relevance(
        page, {cid: {"state": "passed", "profile_hash": PROFILE_HASH, "reason": "profile_match"}})
    store.mark_cursor_committed(page)

    index = JournalDrainIndex.build(
        store, active_profile_hashes={kid: PROFILE_HASH})
    assert index.pending_count([kid]) == 1


def test_page_is_drain_visible_rejects_all_non_drain_states():
    """Only cursor_committed and draining are drain-visible."""
    assert page_is_drain_visible({"state": "cursor_committed"})
    assert page_is_drain_visible({"state": "draining"})
    for bad in ("fetched", "fetching", "planned", "failed", "drained", "", "unknown"):
        assert not page_is_drain_visible({"state": bad})


# ── Phase 1.3: PageProjection delta ───────────────────────────────────


def test_page_replacement_removes_old_and_inserts_new(tmp_path: Path):
    """Replacing a page via add_page fully removes old projections."""
    store = PageJournalStore(tmp_path / "pages")
    kid = keyword_id("测试")
    sig = request_signature(page_size=10)

    page = store.write_page(store.make_synthetic_page(
        page_id="pg", keyword_id=kid, keyword_zh="测试",
        query_id=query_identity("en", "test"), query="test",
        query_language="en", provider="crossref", lane="refresh",
        request_signature_value=sig, request_cursor=INITIAL_CURSOR,
        next_cursor=None, provider_exhausted=True,
        candidates=[PaperCandidate(title="Old", doi="10.1000/test")],
        state="fetched", relevance_profile_hash=PROFILE_HASH))
    page_data = store.read(page)
    cid = page_data["candidates"][0]["candidate_id"]
    store.finalize_relevance(
        page, {cid: {"state": "passed", "profile_hash": PROFILE_HASH, "reason": "profile_match"}})
    store.mark_cursor_committed(page)

    index = JournalDrainIndex.build(
        store, active_profile_hashes={kid: PROFILE_HASH})
    assert index.pending_count([kid]) == 1

    # Replace with emitted → old pending should be gone.
    new_data = store.read(page)
    for c in new_data["candidates"]:
        c["status"] = "emitted"
    new_data["state"] = "cursor_committed"
    index.add_page(page, new_data)

    assert index.pending_count([kid]) == 0
    emitted = index.get_emitted_primary("10.1000/test")
    assert emitted is not None


def test_cross_page_candidate_id_collision_fail_closed(tmp_path: Path):
    """Reusing the same candidate_id across pages must raise."""
    store = PageJournalStore(tmp_path / "pages")
    kid = keyword_id("测试")
    sig = request_signature(page_size=10)

    p1 = store.write_page(store.make_synthetic_page(
        page_id="p1", keyword_id=kid, keyword_zh="测试",
        query_id=query_identity("en", "test"), query="test",
        query_language="en", provider="crossref", lane="refresh",
        request_signature_value=sig, request_cursor=INITIAL_CURSOR,
        next_cursor=None, provider_exhausted=True,
        candidates=[PaperCandidate(title="A", doi="10.1000/a")],
        state="cursor_committed", relevance_profile_hash=PROFILE_HASH))
    p2 = store.write_page(store.make_synthetic_page(
        page_id="p2", keyword_id=kid, keyword_zh="测试",
        query_id=query_identity("en", "test"), query="test",
        query_language="en", provider="crossref", lane="refresh",
        request_signature_value=sig, request_cursor=INITIAL_CURSOR,
        next_cursor=None, provider_exhausted=True,
        candidates=[PaperCandidate(title="B", doi="10.1000/b")],
        state="cursor_committed", relevance_profile_hash=PROFILE_HASH))

    # Read both pages
    d1 = store.read(p1)
    d2 = store.read(p2)

    # Give p2's candidate the same ID as p1's
    cid_p1 = d1["candidates"][0]["candidate_id"]
    d2["candidates"][0]["candidate_id"] = cid_p1

    index = JournalDrainIndex.build(
        store, active_profile_hashes={kid: PROFILE_HASH})

    with pytest.raises(JournalCorruptError, match="candidate_id collision"):
        index.add_page(p2, d2)


def test_failed_add_page_delta_zero_pollution(tmp_path: Path):
    """A failing add_page must leave the index completely unchanged."""
    store = PageJournalStore(tmp_path / "pages")
    kid = keyword_id("测试")

    p1 = store.write_page(store.make_synthetic_page(
        page_id="p1", keyword_id=kid, keyword_zh="测试",
        query_id=query_identity("en", "test"), query="test",
        query_language="en", provider="crossref", lane="refresh",
        request_signature_value=request_signature(page_size=10),
        request_cursor=INITIAL_CURSOR, next_cursor=None,
        provider_exhausted=True,
        candidates=[PaperCandidate(title="Good", doi="10.1000/good")],
        state="cursor_committed", relevance_profile_hash=PROFILE_HASH))

    index = JournalDrainIndex.build(
        store, active_profile_hashes={kid: PROFILE_HASH})
    before_pending = index.pending_count([kid])
    before_emitted = dict(index._emitted_by_id_snapshot())
    before_terminal = dict(index._terminal_by_id_snapshot())

    # Try to add a page with invalid structure.
    with pytest.raises(Exception):
        index.add_page(p1, {"not": "a valid page"})

    # Index must be identical.
    assert index.pending_count([kid]) == before_pending
    assert index._emitted_by_id_snapshot() == before_emitted
    assert index._terminal_by_id_snapshot() == before_terminal


# ── Phase 1.4: Reader concurrency safety ──────────────────────────────


def test_concurrent_readers_see_consistent_snapshots(tmp_path: Path):
    """Multiple reader calls never see a partially-mutated index state."""
    import threading

    store = PageJournalStore(tmp_path / "pages")
    kid = keyword_id("测试")
    page = store.write_page(store.make_synthetic_page(
        page_id="pg", keyword_id=kid, keyword_zh="测试",
        query_id=query_identity("en", "test"), query="test",
        query_language="en", provider="crossref", lane="refresh",
        request_signature_value=request_signature(page_size=10),
        request_cursor=INITIAL_CURSOR, next_cursor=None,
        provider_exhausted=True,
        candidates=[PaperCandidate(title="T", doi="10.1000/test")],
        state="cursor_committed", relevance_profile_hash=PROFILE_HASH))
    page_data = store.read(page)
    for c in page_data["candidates"]:
        c["relevance"]["state"] = "passed"

    index = JournalDrainIndex.build(
        store, active_profile_hashes={kid: PROFILE_HASH})

    errors: list[str] = []

    def reader() -> None:
        for _ in range(100):
            ref = index.get_candidate_ref(
                page_data["candidates"][0]["candidate_id"])
            if ref is None:
                errors.append("candidate_ref returned None")
                return
            emitted = index.get_emitted_primary("10.1000/test")
            _ = index.pending_count([kid])
            _ = index.get_processing_owner("10.1000/test")
            _ = index.get_page_keyword_id(page)

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()

    # Simultaneously mutate.
    for _ in range(50):
        index.update_candidate(page, page_data["candidates"][0])

    for t in threads:
        t.join()

    assert not errors


# ── Phase 1.7: Invariant validation ───────────────────────────────────


def test_validate_clean_index_passes(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    kid = keyword_id("测试")
    store.write_page(store.make_synthetic_page(
        page_id="pg", keyword_id=kid, keyword_zh="测试",
        query_id=query_identity("en", "test"), query="test",
        query_language="en", provider="crossref", lane="refresh",
        request_signature_value=request_signature(page_size=10),
        request_cursor=INITIAL_CURSOR, next_cursor=None,
        provider_exhausted=True,
        candidates=[PaperCandidate(title="T", doi="10.1000/test")],
        state="cursor_committed", relevance_profile_hash=PROFILE_HASH))

    index = JournalDrainIndex.build(
        store, active_profile_hashes={kid: PROFILE_HASH})
    violations = validate_journal_drain_index(index)
    assert violations == []


# ── Helpers ────────────────────────────────────────────────────────────

# Provide internal snapshot helpers for test assertions without exposing
# raw mutable fields.
def _emitted_by_id_snapshot_patch():
    """Monkey-patch snapshot accessors onto JournalDrainIndex for tests."""
    def emitted_snap(self):
        with self._lock:
            return dict(self._state.emitted_by_doi)
    def terminal_snap(self):
        with self._lock:
            return {doi: list(cids) for doi, cids in self._state.terminal_by_doi.items()}
    JournalDrainIndex._emitted_by_id_snapshot = emitted_snap  # type: ignore[attr-defined]
    JournalDrainIndex._terminal_by_id_snapshot = terminal_snap  # type: ignore[attr-defined]


# Import the module for the stable-selection unit test.
import src.discovery.contracts.page_journal as page_journal_module

_emitted_by_id_snapshot_patch()
