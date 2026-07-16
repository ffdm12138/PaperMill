from pathlib import Path

from src.discovery.constants import INITIAL_CURSOR
from src.discovery.keyword_notebook import keyword_id, query_identity
from src.discovery.models import PaperCandidate
from src.discovery.page_journal import JournalDrainIndex, PageJournalStore, request_signature
from tests.helpers.relevance_profiles import relevance_profile


def test_old_profile_passed_candidate_is_not_claimable_after_profile_binding(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    old = relevance_profile(object_term="old sand")
    new = relevance_profile(object_term="new sand")
    kid = keyword_id("风沙动力学")
    page = store.make_page(
        page_id="old-profile", keyword_id=kid, keyword_zh="风沙动力学",
        query_id=query_identity("en", "sand"), query="sand", query_language="en",
        provider="crossref", lane="backfill", generation=37,
        request_signature_value=request_signature(
            filters={"profile_hash": old["profile_hash"]}, page_size=10),
        request_cursor=INITIAL_CURSOR, next_cursor=None, provider_exhausted=True,
        candidates=[PaperCandidate(title="Old sand transport", doi="10.1/old")],
        state="cursor_committed", relevance_profile_hash=old["profile_hash"],
    )
    page["candidates"][0]["relevance"]["state"] = "passed"
    path = store.write_page(page)
    index = JournalDrainIndex.build(
        store, active_profile_hashes={kid: new["profile_hash"]},
    )
    assert index.pending_count([kid]) == 0
    assert store.claim_candidates_from_page(
        path, worker_id="w", lease_seconds=30,
        expected_profile_hash=new["profile_hash"],
    ) == []


def test_full_rebuild_preserves_authoritative_active_profile_bindings(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    profile = relevance_profile(object_term="new sand")
    kid = keyword_id("风沙动力学")
    first = JournalDrainIndex.build(
        store, active_profile_hashes={kid: profile["profile_hash"]},
    )

    rebuilt = JournalDrainIndex.build(
        store, active_profile_hashes=first.active_profile_hashes,
    )

    assert rebuilt.active_profile_hashes == first.active_profile_hashes


def test_durable_emitted_doi_survives_profile_rebinding(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    old = relevance_profile(object_term="old sand")
    new = relevance_profile(object_term="new sand")
    kid = keyword_id("风沙动力学")
    page = store.make_page(
        page_id="historical", keyword_id=kid, keyword_zh="风沙动力学",
        query_id=query_identity("en", "sand"), query="sand", query_language="en",
        provider="crossref", lane="refresh",
        request_signature_value=request_signature(
            filters={"profile_hash": old["profile_hash"]}, page_size=10),
        request_cursor=INITIAL_CURSOR, next_cursor=None, provider_exhausted=True,
        candidates=[PaperCandidate(title="Old sand transport", doi="10.1/durable")],
        state="cursor_committed", relevance_profile_hash=old["profile_hash"],
    )
    page["candidates"][0]["relevance"]["state"] = "passed"
    page["candidates"][0]["status"] = "emitted"
    store.write_page(page)

    index = JournalDrainIndex.build(
        store, active_profile_hashes={kid: new["profile_hash"]},
    )
    assert "10.1/durable" in index.emitted_by_doi
    index.bind_active_profile(kid, new["profile_hash"])
    assert "10.1/durable" in index.emitted_by_doi
