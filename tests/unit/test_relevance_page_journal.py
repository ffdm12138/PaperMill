from pathlib import Path

from src.discovery.constants import INITIAL_CURSOR
from src.discovery.contracts.notebook import keyword_id, query_identity
from src.discovery.models import PaperCandidate
from src.discovery.contracts.page_journal import request_signature
from src.discovery.stores.page_journal_store import PageJournalStoreV4 as PageJournalStore
from tests.helpers.relevance_profiles import relevance_profile


def test_profile_change_closes_passed_deferred_and_unbound_without_using_lane_generation(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    old = relevance_profile(object_term="old sand")
    new = relevance_profile(object_term="new sand")
    page = store.make_synthetic_page(
        page_id="mixed", keyword_id=keyword_id("风沙动力学"), keyword_zh="风沙动力学",
        query_id=query_identity("en", "sand"), query="sand", query_language="en",
        provider="crossref", lane="backfill", generation=99,
        request_signature_value=request_signature(
            filters={"profile_hash": old["profile_hash"]}, page_size=10),
        request_cursor=INITIAL_CURSOR, next_cursor=None, provider_exhausted=True,
        candidates=[PaperCandidate(title=f"P{i}", doi=f"10.1/{i}") for i in range(3)],
        state="cursor_committed", relevance_profile_hash=old["profile_hash"],
    )
    for item, state in zip(page["candidates"], ["passed", "verification_deferred", "profile_unbound"]):
        item["relevance"]["state"] = state
    path = store.write_page(page)
    closed = store.close_stale_profile_candidates(
        path, new_profile_hash=new["profile_hash"],
        planned_mutations=tuple({"candidate_id": item["candidate_id"],
                                 "mutation_id": f"mutation-{index}"}
                                for index, item in enumerate(page["candidates"])),
        closure_timestamp="2026-01-01T00:00:00+00:00",
        transaction_id="transaction-fixed",
    )
    assert {item["relevance"]["state"] for item in closed["candidates"]} == {"rejected"}
    assert closed["state"] == "drained"
