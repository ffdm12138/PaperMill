from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.discovery.models import PaperCandidate
from src.discovery.keyword_notebook import keyword_id, query_identity
from src.discovery.page_journal import (
    INITIAL_CURSOR,
    JournalCorruptError,
    InvalidStateTransition,
    PageJournalStore,
    backfill_page_id,
    candidate_id,
    refresh_page_id,
    request_signature,
)


pytestmark = pytest.mark.unit

KEYWORD_ZH = "风吹雪"
KEYWORD_ID = keyword_id(KEYWORD_ZH)
QUERY = "blowing snow"
QUERY_ID = query_identity("en", QUERY)


def _candidate(**kwargs) -> PaperCandidate:
    return PaperCandidate(title=kwargs.get("title", "A title"), doi=kwargs.get("doi", "10.1234/example"), source_id=kwargs.get("source_id", ""), raw=kwargs.get("raw", {}))


def test_backfill_and_refresh_page_ids_have_distinct_identity_rules():
    sig = request_signature(page_size=25)["hash"]
    backfill_a = backfill_page_id(keyword_id=KEYWORD_ID, query_id=QUERY_ID, provider="openalex", request_signature_hash=sig, request_cursor=INITIAL_CURSOR)
    backfill_b = backfill_page_id(keyword_id=KEYWORD_ID, query_id=QUERY_ID, provider="openalex", request_signature_hash=sig, request_cursor=INITIAL_CURSOR)
    refresh_a = refresh_page_id(keyword_id=KEYWORD_ID, query_id=QUERY_ID, provider="openalex", request_signature_hash=sig, refresh_run_id="run-1", page_sequence=0)
    refresh_b = refresh_page_id(keyword_id=KEYWORD_ID, query_id=QUERY_ID, provider="openalex", request_signature_hash=sig, refresh_run_id="run-2", page_sequence=0)
    assert backfill_a == backfill_b
    assert refresh_a != refresh_b
    assert backfill_a != refresh_a


def test_candidate_id_priority_is_stable_and_namespaced_by_page():
    by_provider = candidate_id("page-a", _candidate(raw={"id": "W123"}, doi="10.9999/other"), 7)
    assert by_provider == candidate_id("page-a", _candidate(raw={"id": "W123"}, doi="10.8888/changed"), 8)
    assert by_provider != candidate_id("page-b", _candidate(raw={"id": "W123"}), 7)

    by_doi = candidate_id("page-a", _candidate(doi="HTTPS://DOI.ORG/10.1234/ABC", title="ignored"), 0)
    assert by_doi == candidate_id("page-a", _candidate(doi="10.1234/abc", title="changed"), 99)

    by_title = candidate_id("page-a", _candidate(doi="", title="  Same   Title "), 3)
    assert by_title == candidate_id("page-a", _candidate(doi="", title="same title"), 3)
    assert by_title != candidate_id("page-a", _candidate(doi="", title="same title"), 4)


def test_page_and_candidate_state_transitions_are_validated(tmp_path: Path):
    store = PageJournalStore(tmp_path)
    sig = request_signature(page_size=10)
    page = store.make_synthetic_page(
        page_id="p1",
        keyword_id=KEYWORD_ID,
        keyword_zh=KEYWORD_ZH,
        query_id=QUERY_ID,
        query=QUERY,
        query_language="en",
        provider="openalex",
        lane="backfill",
        request_signature_value=sig,
        request_cursor=INITIAL_CURSOR,
        next_cursor="next",
        provider_exhausted=False,
        relevance_profile_hash="test-hash",
        candidates=[_candidate()],
    )
    path = store.write_page(page)
    store.finalize_relevance(path, {
        page["candidates"][0]["candidate_id"]: {
            "state": "passed", "profile_hash": "test-hash",
            "reason": "profile_match",
        }
    })
    store.mark_cursor_committed(path)
    cid = store.read(path)["candidates"][0]["candidate_id"]
    claim = store.claim_candidate(path, candidate_id_value=cid, worker_id="w1", lease_seconds=60, expected_profile_hash="test-hash")
    assert claim.claimed
    store.commit_candidate(path, candidate_id_value=cid, worker_id="w1", new_status="emitted")
    assert store.read(path)["state"] == "drained"
    with pytest.raises(InvalidStateTransition):
        store.transition_page(path, "draining")


def test_update_candidate_payload_preserves_identity_and_statistics(tmp_path: Path):
    store = PageJournalStore(tmp_path)
    sig = request_signature(page_size=10)
    page = store.make_synthetic_page(
        page_id="p1",
        keyword_id=KEYWORD_ID,
        keyword_zh=KEYWORD_ZH,
        query_id=QUERY_ID,
        query=QUERY,
        query_language="en",
        provider="openalex",
        lane="backfill",
        request_signature_value=sig,
        request_cursor=INITIAL_CURSOR,
        next_cursor="next",
        provider_exhausted=False,
        relevance_profile_hash="test-hash",
        candidates=[_candidate(doi="", title="Needs DOI")],
    )
    path = store.write_page(page)
    store.finalize_relevance(path, {
        page["candidates"][0]["candidate_id"]: {
            "state": "passed", "profile_hash": "test-hash",
            "reason": "profile_match",
        }
    })
    store.mark_cursor_committed(path)
    before = store.read(path)
    item = before["candidates"][0]
    cid = item["candidate_id"]
    assert store.claim_candidate(path, candidate_id_value=cid, worker_id="w1", lease_seconds=60, expected_profile_hash="test-hash").claimed
    claimed_stats = store.read(path)["statistics"]

    payload = dict(item["candidate"])
    payload["doi"] = "10.1234/resolved"
    payload["doi_resolution"] = {"provider": "crossref", "raw_record": {"DOI": "10.1234/resolved"}}
    updated = store.update_candidate_payload(
        path,
        candidate_id_value=cid,
        worker_id="w1",
        candidate_payload=payload,
    )
    after = store.read(path)
    assert updated["candidate_id"] == cid
    assert after["candidates"][0]["candidate_id"] == cid
    assert after["statistics"] == claimed_stats
    assert after["candidates"][0]["attempts"] == 1
    assert after["candidates"][0]["candidate"]["doi"] == "10.1234/resolved"


def test_corrupt_journal_fails_closed(tmp_path: Path):
    path = tmp_path / "kw" / "exp" / "openalex" / "backfill" / "bad.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": "1.0", "state": "cursor_committed"}), encoding="utf-8")
    with pytest.raises(JournalCorruptError):
        PageJournalStore(tmp_path).read(path)


# ── v2 exact schema validation ────────────────────────────────────────


def _valid_page(store: PageJournalStore, **overrides: Any) -> dict[str, Any]:
    return store.make_synthetic_page(
        page_id="p1",
        keyword_id=KEYWORD_ID,
        keyword_zh=KEYWORD_ZH,
        query_id=QUERY_ID,
        query=QUERY,
        query_language="en",
        provider="openalex",
        lane="backfill",
        request_signature_value=request_signature(page_size=10),
        request_cursor=INITIAL_CURSOR,
        next_cursor="next",
        provider_exhausted=False,
        candidates=[_candidate()],
        **overrides,
    )


def test_page_v2_unknown_field_is_blocked(tmp_path: Path):
    store = PageJournalStore(tmp_path)
    page = _valid_page(store)
    page["unknown_field"] = "x"
    with pytest.raises(JournalCorruptError, match="unexpected fields"):
        store.write_page(page)


def test_page_v2_missing_field_is_blocked(tmp_path: Path):
    store = PageJournalStore(tmp_path)
    page = _valid_page(store)
    page.pop("statistics")
    with pytest.raises(JournalCorruptError, match="missing keys"):
        store.write_page(page)


@pytest.mark.parametrize("value", [0, -1, "1", False])
def test_page_v2_invalid_generation_is_blocked(tmp_path: Path, value):
    store = PageJournalStore(tmp_path)
    page = _valid_page(store)
    page["generation"] = value
    with pytest.raises(JournalCorruptError, match="generation"):
        store.write_page(page)


def test_page_v2_valid_generation_is_preserved(tmp_path: Path):
    store = PageJournalStore(tmp_path)
    page = _valid_page(store, generation=3)
    written = store.write_page(page)
    assert store.read(written)["generation"] == 3


def test_page_v2_valid_document_roundtrips_exactly(tmp_path: Path):
    store = PageJournalStore(tmp_path)
    page = _valid_page(store)
    written = store.write_page(page)
    read = store.read(written)
    assert set(read) == set(page)
    for key in read:
        assert read[key] == page[key]
