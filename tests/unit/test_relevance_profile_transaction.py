import json
import hashlib
from pathlib import Path

import pytest

from src.discovery.constants import INITIAL_CURSOR
from src.discovery.keyword_notebook import KeywordNotebookStore, query_identity
from src.discovery.models import PaperCandidate
from src.discovery.page_journal import PageJournalStore, request_signature
from src.discovery.relevance_profiles import (
    RelevanceProfilePlanError,
    RelevanceProfileTransactionError,
    TaxonomySnapshot,
    apply_relevance_profile_plan,
    build_relevance_profile_plan,
    resume_relevance_profile_transaction,
)
from tests.helpers.relevance_profiles import relevance_profile


def _taxonomy() -> TaxonomySnapshot:
    entity = {
        "id": "https://openalex.org/subfields/S1", "display_name": "Test Subfield",
        "field": {"id": "https://openalex.org/fields/1", "display_name": "Field"},
        "domain": {"id": "https://openalex.org/domains/1", "display_name": "Domain"},
    }
    # Build a fully self-consistent snapshot using the same hash helpers
    # as fetch_subfields_taxonomy so validate_taxonomy_snapshot() passes.
    from src.discovery.relevance_profiles import (
        _json_bytes, _canonical_hash, _rebuild_canonical_entities,
    )
    pages = [{"results": [entity], "meta": {"next_cursor": None}}]
    page_hashes = tuple(_canonical_hash(p) for p in pages)
    snapshot_sha = _canonical_hash({"pages": pages, "entities": [entity]})
    raw_sha = hashlib.sha256(b"".join(_json_bytes(p) for p in pages)).hexdigest()
    canonical = _rebuild_canonical_entities(pages)
    semantic_sha = _canonical_hash({"entities": canonical})
    return TaxonomySnapshot(
        pages=tuple(pages), entities=(entity,), retrieved_at="2026-07-17T00:00:00+00:00",
        page_hashes=page_hashes, snapshot_sha256=snapshot_sha,
        schema_version="1.0",
        raw_snapshot_sha256=raw_sha,
        taxonomy_semantic_sha256=semantic_sha,
    )


def _setup(tmp_path: Path):
    notebooks = tmp_path / "notebooks"
    pages = tmp_path / "pages"
    transactions = tmp_path / "transactions" / "relevance_profiles"
    store = KeywordNotebookStore(notebooks)
    store.create_notebook(
        "风沙动力学", enabled=False,
        search_queries=[
            {"query": "风沙动力学", "language": "zh"},
            {"query": "aeolian dynamics", "language": "en"},
        ],
    )
    old = relevance_profile(object_term="old sand")
    store.set_relevance_profile("风沙动力学", old, generation=2)
    store.set_enabled("风沙动力学", True)
    nb = store.require_v3("风沙动力学")
    journal = PageJournalStore(pages)
    page = journal.make_synthetic_page(
        page_id="old", keyword_id=nb["keyword_id"], keyword_zh=nb["keyword_zh"],
        query_id=query_identity("en", "aeolian dynamics"), query="aeolian dynamics",
        query_language="en", provider="crossref", lane="backfill", generation=41,
        request_signature_value=request_signature(
            filters={"profile_hash": old["profile_hash"]}, page_size=10),
        request_cursor=INITIAL_CURSOR, next_cursor=None, provider_exhausted=True,
        candidates=[PaperCandidate(title="Old sand transport", doi="10.1/old")],
        state="cursor_committed", relevance_profile_hash=old["profile_hash"],
    )
    page["candidates"][0]["relevance"]["state"] = "passed"
    page_path = journal.write_page(page)
    new = relevance_profile(object_term="new sand")
    profiles = tmp_path / "profiles.json"
    profiles.write_text(json.dumps({
        "profiles": [{"keyword_zh": "风沙动力学", "profile": new}]
    }), encoding="utf-8")
    return notebooks, pages, transactions, page_path, profiles, new


def test_plan_closes_old_passed_candidate_independent_of_backfill_generation(tmp_path: Path):
    notebooks, pages, transactions, page_path, profiles, new = _setup(tmp_path)
    plan = build_relevance_profile_plan(
        profiles_path=profiles, notebook_dir=notebooks, pending_pages_dir=pages,
        transaction_root=transactions, taxonomy=_taxonomy(),
    )
    assert plan["notebooks"][0]["page_mutations"][0]["candidate_count"] == 1
    result = apply_relevance_profile_plan(plan, expected_plan_hash=plan["plan_hash"])
    assert result["state"] == "committed"
    candidate = PageJournalStore(pages).read(page_path)["candidates"][0]
    assert candidate["relevance"]["state"] == "rejected"
    assert candidate["relevance"]["verification"]["status"] == "profile_superseded"
    assert KeywordNotebookStore(notebooks).require_v3("风沙动力学")["relevance_profile"]["profile_hash"] == new["profile_hash"]


def test_apply_refuses_second_plan_while_foreign_journal_is_applying(tmp_path: Path):
    notebooks, pages, transactions, _page_path, profiles, _new = _setup(tmp_path)
    plan = build_relevance_profile_plan(
        profiles_path=profiles, notebook_dir=notebooks, pending_pages_dir=pages,
        transaction_root=transactions, taxonomy=_taxonomy(),
    )
    transactions.mkdir(parents=True, exist_ok=True)
    (transactions / "foreign.json").write_text(json.dumps({
        "transaction_id": "foreign", "state": "applying",
    }), encoding="utf-8")
    with pytest.raises(RelevanceProfileTransactionError, match="resume it first"):
        apply_relevance_profile_plan(plan, expected_plan_hash=plan["plan_hash"])


@pytest.mark.parametrize("terminal_status", [
    "staged", "emitted", "existing_duplicate", "duplicate_observation",
    "invalid_doi", "unresolved", "failed_terminal",
])
def test_profile_apply_never_rewrites_completed_terminal_relevance(
    tmp_path: Path, terminal_status: str,
):
    notebooks, pages, transactions, page_path, profiles, _new = _setup(tmp_path)
    journal = PageJournalStore(pages)
    page = journal.read(page_path)
    page["candidates"][0]["status"] = terminal_status
    page["candidates"][0]["terminal_reason"] = terminal_status
    page_path.write_text(json.dumps(page, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    before_candidate = json.dumps(
        page["candidates"][0], ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")

    plan = build_relevance_profile_plan(
        profiles_path=profiles, notebook_dir=notebooks, pending_pages_dir=pages,
        transaction_root=transactions, taxonomy=_taxonomy(),
    )
    assert plan["notebooks"][0]["historical_terminal_untouched"] == 1
    assert plan["notebooks"][0]["page_mutations"] == []
    apply_relevance_profile_plan(plan, expected_plan_hash=plan["plan_hash"])
    after = PageJournalStore(pages).read(page_path)["candidates"][0]
    assert json.dumps(after, ensure_ascii=False, sort_keys=True).encode("utf-8") == before_candidate


def test_last_page_drift_causes_zero_transaction_or_page_writes(tmp_path: Path):
    notebooks, pages, transactions, first_path, profiles, _new = _setup(tmp_path)
    first_page = PageJournalStore(pages).read(first_path)
    second_page = json.loads(json.dumps(first_page))
    second_page["page_id"] = "second"
    second_page["candidates"][0]["candidate_id"] = "second-candidate"
    second_path = PageJournalStore(pages).write_page(second_page)
    plan = build_relevance_profile_plan(
        profiles_path=profiles, notebook_dir=notebooks, pending_pages_dir=pages,
        transaction_root=transactions, taxonomy=_taxonomy(),
    )
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in (first_path, second_path)}
    second_page = PageJournalStore(pages).read(second_path)
    second_page["candidates"][0]["last_error"] = "drift"
    second_path.write_text(json.dumps(second_page, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    drift_hash = hashlib.sha256(second_path.read_bytes()).hexdigest()

    with pytest.raises(RelevanceProfileTransactionError, match="page drift"):
        apply_relevance_profile_plan(plan, expected_plan_hash=plan["plan_hash"])

    assert hashlib.sha256(first_path.read_bytes()).hexdigest() == before[first_path]
    assert hashlib.sha256(second_path.read_bytes()).hexdigest() == drift_hash
    assert not transactions.exists() or list(transactions.iterdir()) == []


def test_unknown_lifecycle_globally_blocks_plan_with_non_applicable_report(tmp_path: Path):
    notebooks, pages, transactions, page_path, profiles, _new = _setup(tmp_path)
    page = json.loads(page_path.read_text(encoding="utf-8"))
    page["candidates"][0]["status"] = "future_unreviewed_state"
    page_path.write_text(json.dumps(page, ensure_ascii=False, indent=2), encoding="utf-8")

    with pytest.raises(RelevanceProfilePlanError) as caught:
        build_relevance_profile_plan(
            profiles_path=profiles, notebook_dir=notebooks, pending_pages_dir=pages,
            transaction_root=transactions, taxonomy=_taxonomy(),
        )

    report = caught.value.report
    assert report["status"] == "failed"
    assert report["applicable"] is False
    assert report["unknown_lifecycle_candidates"][0]["status"] == "future_unreviewed_state"
    assert not transactions.exists()


@pytest.mark.parametrize("status", ["processing", "failed_retryable"])
def test_recovery_required_old_profile_candidate_blocks_whole_plan(
    tmp_path: Path, status: str,
):
    notebooks, pages, transactions, page_path, profiles, _new = _setup(tmp_path)
    page = PageJournalStore(pages).read(page_path)
    page["candidates"][0]["status"] = status
    page_path.write_text(json.dumps(page, ensure_ascii=False, indent=2), encoding="utf-8")

    with pytest.raises(RelevanceProfilePlanError) as caught:
        build_relevance_profile_plan(
            profiles_path=profiles, notebook_dir=notebooks, pending_pages_dir=pages,
            transaction_root=transactions, taxonomy=_taxonomy(),
        )
    assert caught.value.report["recovery_required_candidates"][0]["status"] == status
    assert caught.value.report["applicable"] is False
    assert not transactions.exists()


def test_page_crash_resumes_from_exact_expected_after_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    notebooks, pages, transactions, first_path, profiles, _new = _setup(tmp_path)
    first_page = PageJournalStore(pages).read(first_path)
    second_page = json.loads(json.dumps(first_page))
    second_page["page_id"] = "second"
    second_page["candidates"][0]["candidate_id"] = "second-candidate"
    second_path = PageJournalStore(pages).write_page(second_page)
    plan = build_relevance_profile_plan(
        profiles_path=profiles, notebook_dir=notebooks, pending_pages_dir=pages,
        transaction_root=transactions, taxonomy=_taxonomy(),
    )
    original = PageJournalStore.close_stale_profile_candidates
    calls = 0

    def crash_on_second(self, path, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic page crash")
        return original(self, path, **kwargs)

    monkeypatch.setattr(PageJournalStore, "close_stale_profile_candidates", crash_on_second)
    with pytest.raises(RuntimeError, match="synthetic page crash"):
        apply_relevance_profile_plan(plan, expected_plan_hash=plan["plan_hash"])
    transaction = transactions / f"{plan['transaction_id']}.json"
    journal = json.loads(transaction.read_text(encoding="utf-8"))
    assert journal["state"] == "applying"
    assert sum(bool(item["applied"]) for item in journal["page_mutations"]) == 1

    monkeypatch.setattr(PageJournalStore, "close_stale_profile_candidates", original)
    result = resume_relevance_profile_transaction(transaction)
    assert result["state"] == "committed"
    assert PageJournalStore(pages).read(first_path)["candidates"][0]["relevance"]["state"] == "rejected"
    assert PageJournalStore(pages).read(second_path)["candidates"][0]["relevance"]["state"] == "rejected"
    assert resume_relevance_profile_transaction(transaction)["state"] == "committed"
