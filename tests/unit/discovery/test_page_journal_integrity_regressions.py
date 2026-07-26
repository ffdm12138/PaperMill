from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from src.discovery.backfill_transaction import run_backfill_page_transaction
from src.discovery.constants import INITIAL_CURSOR
from src.discovery.execution.lane_models import DiscoveryLaneKey, LaneExecutionSpec, RequestSignature
from src.discovery.contracts.notebook import keyword_id, query_identity
from src.discovery.models import PaperCandidate
from src.discovery.contracts.page_journal import (
    InvalidStateTransition,
    JournalCorruptError,
    backfill_page_id,
    request_signature,
)
from src.discovery.stores.page_journal_store import PageJournalStoreV4 as PageJournalStore
from src.discovery.providers.provider_page_fetcher import CallbackProviderPageFetcher


KEYWORD_ZH = "风沙"
KEYWORD_ID = keyword_id(KEYWORD_ZH)
QUERY = "风沙输运"
QUERY_ID = query_identity("zh", QUERY)
PROVIDER = "openalex"


def _spec(signature: dict, *, generation: int) -> LaneExecutionSpec:
    typed_signature = RequestSignature.from_dict_strict(signature)
    return LaneExecutionSpec(
        key=DiscoveryLaneKey(
            keyword_id=KEYWORD_ID,
            query_id=QUERY_ID,
            provider=PROVIDER,
            mode="backfill",
            generation=generation,
            request_signature=typed_signature.hash,
        ),
        request_signature=typed_signature,
        keyword_zh=KEYWORD_ZH,
        query=QUERY,
        query_language="zh",
        relevance_profile_hash="test-profile",
    )


def _never_fetcher() -> CallbackProviderPageFetcher:
    return CallbackProviderPageFetcher(
        lambda _spec, _cursor, _client: pytest.fail("provider must not be called"),
    )


def _page(
    store: PageJournalStore,
    *,
    generation: int = 1,
    state: str = "cursor_committed",
    next_cursor: str | None = "next",
    provider_exhausted: bool = False,
) -> tuple[dict, dict]:
    signature = request_signature(page_size=25)
    page_id = backfill_page_id(
        keyword_id=KEYWORD_ID,
        query_id=QUERY_ID,
        provider=PROVIDER,
        request_signature_hash=signature["hash"],
        request_cursor=INITIAL_CURSOR,
    )
    return store.make_synthetic_page(
        page_id=page_id,
        keyword_id=KEYWORD_ID,
        keyword_zh=KEYWORD_ZH,
        query_id=QUERY_ID,
        query=QUERY,
        query_language="zh",
        provider=PROVIDER,
        lane="backfill",
        generation=generation,
        request_signature_value=signature,
        request_cursor=INITIAL_CURSOR,
        next_cursor=next_cursor,
        provider_exhausted=provider_exhausted,
        candidates=[],
        state=state,
    ), signature


def _processing_candidate() -> dict:
    return {
        "candidate_id": "candidate-1",
        "candidate": {"doi": "10.1000/integrity"},
        "status": "processing",
        "claimed_by": "owner",
        "claimed_at": "2026-01-01T00:00:00+00:00",
        "lease_expires_at": "2099-01-01T00:00:00+00:00",
    }


def test_duplicate_candidate_ids_fail_before_page_is_durable(tmp_path: Path) -> None:
    store = PageJournalStore(tmp_path / "pages")
    page, _signature = _page(store)
    first = {
        "candidate_id": "duplicate-id",
        "candidate": {"doi": "10.1000/one"},
        "status": "pending",
    }
    second = deepcopy(first)
    second["candidate"]["doi"] = "10.1000/two"
    page["candidates"] = [first, second]
    page["returned_count"] = 2

    expected_path = store.page_path(
        keyword_id=KEYWORD_ID,
        query_id=QUERY_ID,
        provider=PROVIDER,
        lane="backfill",
        page_id=page["page_id"],
    )
    with pytest.raises(JournalCorruptError, match="duplicate candidate_id"):
        store.write_page(page)
    assert not expected_path.exists()


def test_terminal_batch_replay_is_exactly_idempotent_and_cannot_overwrite(
    tmp_path: Path,
) -> None:
    store = PageJournalStore(tmp_path / "pages")
    page, _signature = _page(store)
    page["candidates"] = [_processing_candidate()]
    page["returned_count"] = 1
    path = store.write_page(page)
    outcome = {
        "candidate_id": "candidate-1",
        "new_status": "staged",
        "updates": {
            "staged_paper_number": "0000000000000001",
            "terminal_reason": "staged",
        },
    }
    store.commit_candidate_results(path, [outcome], worker_id="owner")
    committed_bytes = path.read_bytes()

    store.commit_candidate_results(path, [deepcopy(outcome)], worker_id="other-worker")
    assert path.read_bytes() == committed_bytes

    conflicting = deepcopy(outcome)
    conflicting["updates"]["staged_paper_number"] = "9999999999999999"
    with pytest.raises(InvalidStateTransition, match="terminal candidate replay"):
        store.commit_candidate_results(path, [conflicting], worker_id="other-worker")
    assert path.read_bytes() == committed_bytes


class _BackfillNotebookStub:
    def __init__(self, state: dict) -> None:
        self.state = dict(state)
        self.commit_calls = 0

    def ensure_backfill_generation(self, *args, **kwargs) -> dict:
        return dict(self.state)

    def get_backfill_state(self, *args, **kwargs) -> dict:
        return dict(self.state)

    def is_backfill_exhausted(self, *args, **kwargs) -> bool:
        return bool(self.state.get("exhausted"))

    def get_backfill_cursor(self, *args, **kwargs) -> str:
        return str(self.state.get("cursor") or INITIAL_CURSOR)

    def commit_backfill_cursor(self, *args, **kwargs):
        self.commit_calls += 1
        raise AssertionError("cursor CAS must not run for a mismatched reused page")


def test_reused_page_generation_mismatch_fails_before_cursor_cas(tmp_path: Path) -> None:
    store = PageJournalStore(tmp_path / "pages")
    stale_page, signature = _page(store, generation=1, state="fetched")
    path = store.write_page(stale_page)
    notebook = _BackfillNotebookStub({
        "generation": 2,
        "request_signature": signature["hash"],
        "cursor": INITIAL_CURSOR,
        "exhausted": False,
        "last_committed_page_id": "",
    })

    result = run_backfill_page_transaction(
        _spec(signature, generation=2),
        notebook_store=notebook,  # type: ignore[arg-type]
        journal_store=store,
        locks_dir=tmp_path / "locks",
        page_fetcher=_never_fetcher(),
        client=object(),  # type: ignore[arg-type]
    )

    assert result.status == "failed_retryable"
    assert result.error_type == "journal_corruption"
    assert result.page_path == path
    assert notebook.commit_calls == 0


def test_recovered_fetched_page_is_returned_when_lane_is_already_exhausted(
    tmp_path: Path,
) -> None:
    store = PageJournalStore(tmp_path / "pages")
    page, signature = _page(
        store,
        generation=1,
        state="fetched",
        next_cursor="next",
        provider_exhausted=True,
    )
    path = store.write_page(page)
    notebook = _BackfillNotebookStub({
        "generation": 1,
        "request_signature": signature["hash"],
        "cursor": "next",
        "exhausted": True,
        "last_committed_page_id": page["page_id"],
        "exhaustion_evidence": page["exhaustion_evidence"],
    })

    result = run_backfill_page_transaction(
        _spec(signature, generation=1),
        notebook_store=notebook,  # type: ignore[arg-type]
        journal_store=store,
        locks_dir=tmp_path / "locks",
        page_fetcher=_never_fetcher(),
        client=object(),  # type: ignore[arg-type]
    )

    assert result.status == "exhausted"
    assert result.recovered_page_paths == (path,)
    assert store.read(path)["state"] == "cursor_committed"


# ── Phase 1: Strict relevance admission ──────────────────────────────


def test_profile_unbound_not_claimable(tmp_path: Path) -> None:
    store = PageJournalStore(tmp_path / "pages")
    page = store.make_synthetic_page(
        page_id="p1", keyword_id=KEYWORD_ID, keyword_zh=KEYWORD_ZH,
        query_id=QUERY_ID, query=QUERY, query_language="zh",
        provider=PROVIDER, lane="backfill",
        request_signature_value=request_signature(page_size=10),
        request_cursor=INITIAL_CURSOR, next_cursor="next",
        provider_exhausted=False,
        relevance_profile_hash="test-hash",
        candidates=[PaperCandidate(title="T", doi="10.1000/test")],
        state="cursor_committed",
    )
    path = store.write_page(page)
    cid = store.read(path)["candidates"][0]["candidate_id"]
    claim = store.claim_candidate(
        path, candidate_id_value=cid, worker_id="w1",
        lease_seconds=60, expected_profile_hash="test-hash",
    )
    assert not claim.claimed
    assert "relevance_not_passed" in claim.reason


def test_passed_wrong_hash_not_claimable(tmp_path: Path) -> None:
    store = PageJournalStore(tmp_path / "pages")
    page = store.make_synthetic_page(
        page_id="p1", keyword_id=KEYWORD_ID, keyword_zh=KEYWORD_ZH,
        query_id=QUERY_ID, query=QUERY, query_language="zh",
        provider=PROVIDER, lane="backfill",
        request_signature_value=request_signature(page_size=10),
        request_cursor=INITIAL_CURSOR, next_cursor="next",
        provider_exhausted=False,
        relevance_profile_hash="test-hash",
        candidates=[PaperCandidate(title="T", doi="10.1000/test")],
        state="cursor_committed",
    )
    page["candidates"][0]["relevance"]["state"] = "passed"
    path = store.write_page(page)
    cid = store.read(path)["candidates"][0]["candidate_id"]
    claim = store.claim_candidate(
        path, candidate_id_value=cid, worker_id="w1",
        lease_seconds=60, expected_profile_hash="wrong-hash",
    )
    assert not claim.claimed
    assert "relevance_not_passed" in claim.reason


def test_passed_correct_hash_claimable(tmp_path: Path) -> None:
    store = PageJournalStore(tmp_path / "pages")
    page = store.make_synthetic_page(
        page_id="p1", keyword_id=KEYWORD_ID, keyword_zh=KEYWORD_ZH,
        query_id=QUERY_ID, query=QUERY, query_language="zh",
        provider=PROVIDER, lane="backfill",
        request_signature_value=request_signature(page_size=10),
        request_cursor=INITIAL_CURSOR, next_cursor="next",
        provider_exhausted=False,
        relevance_profile_hash="test-hash",
        candidates=[PaperCandidate(title="T", doi="10.1000/test")],
        state="cursor_committed",
    )
    page["candidates"][0]["relevance"]["state"] = "passed"
    path = store.write_page(page)
    cid = store.read(path)["candidates"][0]["candidate_id"]
    claim = store.claim_candidate(
        path, candidate_id_value=cid, worker_id="w1",
        lease_seconds=60, expected_profile_hash="test-hash",
    )
    assert claim.claimed


def test_new_page_profile_unbound_rejects_cursor_commit(tmp_path: Path) -> None:
    store = PageJournalStore(tmp_path / "pages")
    page = store.make_synthetic_page(
        page_id="p1", keyword_id=KEYWORD_ID, keyword_zh=KEYWORD_ZH,
        query_id=QUERY_ID, query=QUERY, query_language="zh",
        provider=PROVIDER, lane="backfill",
        request_signature_value=request_signature(page_size=10),
        request_cursor=INITIAL_CURSOR, next_cursor="next",
        provider_exhausted=False,
        relevance_profile_hash="test-hash",
        candidates=[PaperCandidate(title="T", doi="10.1000/test")],
    )
    page_path = store.write_page(page)
    with pytest.raises(InvalidStateTransition, match="profile_unbound"):
        store.mark_cursor_committed(page_path)


def test_relevance_finalize_then_cursor_commit_succeeds(tmp_path: Path) -> None:
    store = PageJournalStore(tmp_path / "pages")
    page = store.make_synthetic_page(
        page_id="p1", keyword_id=KEYWORD_ID, keyword_zh=KEYWORD_ZH,
        query_id=QUERY_ID, query=QUERY, query_language="zh",
        provider=PROVIDER, lane="backfill",
        request_signature_value=request_signature(page_size=10),
        request_cursor=INITIAL_CURSOR, next_cursor="next",
        provider_exhausted=False,
        relevance_profile_hash="test-hash",
        candidates=[PaperCandidate(title="T", doi="10.1000/test")],
    )
    path = store.write_page(page)
    cid = page["candidates"][0]["candidate_id"]
    store.finalize_relevance(path, {cid: {"state": "passed", "profile_hash": "test-hash", "reason": "profile_match"}})
    page_data = store.mark_cursor_committed(path)
    assert page_data["state"] in {"cursor_committed", "drained"}


def test_legacy_page_no_relevance_blocked_from_cursor_commit(
    tmp_path: Path,
) -> None:
    store = PageJournalStore(tmp_path / "pages")
    # Create a page WITHOUT relevance_profile_hash — no relevance record
    page = store.make_synthetic_page(
        page_id="p1", keyword_id=KEYWORD_ID, keyword_zh=KEYWORD_ZH,
        query_id=QUERY_ID, query=QUERY, query_language="zh",
        provider=PROVIDER, lane="backfill",
        request_signature_value=request_signature(page_size=10),
        request_cursor=INITIAL_CURSOR, next_cursor="next",
        provider_exhausted=False,
        candidates=[PaperCandidate(title="T", doi="10.1000/test")],
    )
    path = store.write_page(page)
    with pytest.raises(InvalidStateTransition, match="missing a relevance record"):
        store.mark_cursor_committed(path)


def test_mixed_language_lane_and_page_are_accepted(tmp_path: Path) -> None:
    """QueryLanguage.MIXED is a distinct identity (enums.py docstring): the
    lane spec and the page journal must accept it — never coerce to zh."""
    mixed_query = "风沙 wind sand transport"
    mixed_qid = query_identity("mixed", mixed_query)
    signature = RequestSignature.from_dict_strict(request_signature(page_size=25))
    spec = LaneExecutionSpec(
        key=DiscoveryLaneKey(
            keyword_id=KEYWORD_ID,
            query_id=mixed_qid,
            provider=PROVIDER,
            mode="backfill",
            generation=1,
            request_signature=signature.hash,
        ),
        request_signature=signature,
        keyword_zh=KEYWORD_ZH,
        query=mixed_query,
        query_language="mixed",
        relevance_profile_hash="test-profile",
    )
    assert spec.query_language == "mixed"

    store = PageJournalStore(tmp_path / "pages")
    page = store.make_synthetic_page(
        page_id="p-mixed", keyword_id=KEYWORD_ID, keyword_zh=KEYWORD_ZH,
        query_id=mixed_qid, query=mixed_query, query_language="mixed",
        provider=PROVIDER, lane="backfill",
        request_signature_value=request_signature(page_size=25),
        request_cursor=INITIAL_CURSOR, next_cursor="next",
        provider_exhausted=False,
        candidates=[],
    )
    assert page["query_language"] == "mixed"
    assert page["query_id"] == mixed_qid
