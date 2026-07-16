from datetime import timedelta
from pathlib import Path

import pytest

import src.discovery.pending_queue as pending_queue_module
from src.discovery.batch_runtime import ActiveRelevanceProfiles, DiscoveryBatchRuntime
from src.discovery.keyword_notebook import keyword_id, query_identity
from src.discovery.models import PaperCandidate
from src.discovery.page_journal import PageJournalStore, request_signature
from src.discovery.pending_queue import drain_pending_candidates
from src.discovery.staging_context import DiscoveryStagingContext
from src.discovery.staging_metrics import CollectingStagingMetricsObserver
from src.services.network_metadata_staging import stage_network_metadata_records

pytestmark = pytest.mark.integration
PROFILE_HASH = "test-active-profile"


def _bind_page(page):
    for candidate in page["candidates"]:
        candidate["relevance"]["state"] = "passed"
    return page


def test_batch_staging_shares_context_lock_ledger_and_keeps_checkpoints(tmp_path: Path):
    metrics = CollectingStagingMetricsObserver()
    context = DiscoveryStagingContext.create_with_observer(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json", observer=metrics)
    records = [{"title": f"Paper {i}", "year": 2026, "doi": f"10.9100/{i}"}
               for i in range(20)]
    report = stage_network_metadata_records(
        records, paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json", apply=True, transaction=context.transaction,
        max_lock_seconds=300)
    assert report["allocated"] == 20
    assert metrics.staging_context_builds == 1
    assert metrics.full_registry_builds == 1
    assert metrics.formal_publication_view_loads == 1
    assert metrics.write_lock_acquisitions == 2
    assert metrics.ledger_loads == 3  # cold build plus one load per lock epoch
    assert metrics.ledger_saves == 40  # reservation + metadata_staged per paper
    assert metrics.batch_sizes == [16, 4]


def test_direct_publish_is_settled_across_separate_lock_epochs(tmp_path: Path):
    metrics = CollectingStagingMetricsObserver()
    context = DiscoveryStagingContext.create_with_observer(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json", observer=metrics)

    first = stage_network_metadata_records(
        [{"title": "First", "year": 2026, "doi": "10.9100/direct.1"}],
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json", apply=True,
        transaction=context.transaction)
    assert first["allocated"] == 1
    first_number = first["items"][0]["paper_number"]
    first_record = context.transaction.registry_snapshot.records_by_number[first_number]
    assert first_record.evidence.ledger_state == "metadata_staged"
    assert first_record.lifecycle.ledger_state == "metadata_staged"
    reads_after_first = metrics.workspace_records_read
    fingerprints_after_first = metrics.workspace_fingerprint_calls

    second = stage_network_metadata_records(
        [{"title": "Second", "year": 2026, "doi": "10.9100/direct.2"}],
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json", apply=True,
        transaction=context.transaction)

    assert second["allocated"] == 1
    assert metrics.workspace_records_read == reads_after_first
    assert metrics.workspace_fingerprint_calls == fingerprints_after_first
    assert metrics.registry_direct_publishes == 2


def test_pending_drain_stages_one_claim_batch_in_one_transaction_and_page_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    journal = PageJournalStore(tmp_path / "pages")
    kid = keyword_id("关键词")
    page = _bind_page(journal.make_page(
        page_id="stage-batch",
        keyword_id=kid,
        keyword_zh="关键词",
        query_id=query_identity("en", "keyword"),
        query="keyword",
        query_language="en",
        provider="crossref",
        lane="refresh",
        request_signature_value=request_signature(page_size=4),
        request_cursor="*",
        next_cursor=None,
        provider_exhausted=True,
        candidates=[
            PaperCandidate(title=f"Paper {index}", year=2026, doi=f"10.9200/drain.{index}")
            for index in range(4)
        ],
        state="cursor_committed", relevance_profile_hash=PROFILE_HASH,
    ))
    page_path = journal.write_page(page)
    runtime = DiscoveryBatchRuntime.create(
        journal=journal,
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        needs_staging=True,
        active_relevance_profiles=ActiveRelevanceProfiles.build({kid: PROFILE_HASH}),
    )
    metrics = runtime.metrics.staging
    ledger_loads_before = metrics.ledger_loads
    ledger_saves_before = metrics.ledger_saves

    stage_batch_sizes: list[int] = []
    real_stage = pending_queue_module.stage_network_metadata_records

    def stage_spy(records, **kwargs):
        stage_batch_sizes.append(len(records))
        return real_stage(records, **kwargs)

    commit_batches: list[tuple[Path, int]] = []
    real_commit_results = journal.commit_candidate_results

    def commit_results_spy(path, results, *, worker_id):
        materialized = list(results)
        commit_batches.append((path, len(materialized)))
        return real_commit_results(path, materialized, worker_id=worker_id)

    def reject_single_commit(*args, **kwargs):
        pytest.fail("authoritative staging drain must use page-level batch commit")

    monkeypatch.setattr(pending_queue_module, "stage_network_metadata_records", stage_spy)
    monkeypatch.setattr(journal, "commit_candidate_results", commit_results_spy)
    monkeypatch.setattr(journal, "commit_candidate", reject_single_commit)

    report = drain_pending_candidates(
        journal=journal,
        keyword_ids=[kid],
        candidate_budget=4,
        stage_to_paper_raw=True,
        apply=True,
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        locks_dir=tmp_path / "locks",
        exports_dir=tmp_path / "exports",
        worker_id="worker",
        runtime=runtime,
    )

    assert report.errors == []
    assert report.staged == 4
    assert stage_batch_sizes == [4]
    assert commit_batches == [(page_path, 4)]
    assert metrics.write_lock_acquisitions == 1
    assert metrics.ledger_loads - ledger_loads_before == 1
    assert metrics.ledger_saves - ledger_saves_before == 8
    assert runtime.metrics.candidate_lease_renewals == 0
    assert runtime.metrics.page_fsyncs == 2  # one page claim plus one page commit
    assert journal.read(page_path)["state"] == "drained"


def test_pending_drain_deduplicates_same_batch_doi_before_authoritative_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    journal = PageJournalStore(tmp_path / "pages")
    kid = keyword_id("关键词")
    doi = "10.9200/same-batch"
    page = _bind_page(journal.make_page(
        page_id="same-batch",
        keyword_id=kid,
        keyword_zh="关键词",
        query_id=query_identity("en", "keyword"),
        query="keyword",
        query_language="en",
        provider="crossref",
        lane="refresh",
        request_signature_value=request_signature(page_size=2),
        request_cursor="*",
        next_cursor=None,
        provider_exhausted=True,
        candidates=[
            PaperCandidate(title=f"Observation {index}", year=2026, doi=doi,
                           source_id=f"crossref-record-{index}")
            for index in range(2)
        ],
        state="cursor_committed", relevance_profile_hash=PROFILE_HASH,
    ))
    page_path = journal.write_page(page)
    runtime = DiscoveryBatchRuntime.create(
        journal=journal,
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        needs_staging=True,
        active_relevance_profiles=ActiveRelevanceProfiles.build({kid: PROFILE_HASH}),
    )
    # A stale in-memory processing hint must not override the DOI file lock and
    # authoritative Registry transaction.
    runtime.journal_index.processing_by_doi[doi] = "stale-owner"

    stage_batch_sizes: list[int] = []
    real_stage = pending_queue_module.stage_network_metadata_records

    def stage_spy(records, **kwargs):
        stage_batch_sizes.append(len(records))
        return real_stage(records, **kwargs)

    commit_batch_sizes: list[int] = []
    real_commit_results = journal.commit_candidate_results

    def commit_results_spy(path, results, *, worker_id):
        materialized = list(results)
        commit_batch_sizes.append(len(materialized))
        return real_commit_results(path, materialized, worker_id=worker_id)

    monkeypatch.setattr(pending_queue_module, "stage_network_metadata_records", stage_spy)
    monkeypatch.setattr(journal, "commit_candidate_results", commit_results_spy)

    report = drain_pending_candidates(
        journal=journal,
        keyword_ids=[kid],
        candidate_budget=2,
        stage_to_paper_raw=True,
        apply=True,
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        locks_dir=tmp_path / "locks",
        exports_dir=tmp_path / "exports",
        worker_id="worker",
        runtime=runtime,
    )

    assert report.errors == []
    assert report.staged == 1
    assert report.duplicate_observation == 1
    assert stage_batch_sizes == [1]
    assert commit_batch_sizes == [2]
    assert runtime.metrics.staging.write_lock_acquisitions == 1
    assert runtime.metrics.staging.ledger_saves == 2
    assert journal.read(page_path)["state"] == "drained"


def test_pending_drain_renews_only_after_half_lease_threshold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    journal = PageJournalStore(tmp_path / "pages")
    kid = keyword_id("关键词")
    page = _bind_page(journal.make_page(
        page_id="slow-lease",
        keyword_id=kid,
        keyword_zh="关键词",
        query_id=query_identity("en", "keyword"),
        query="keyword",
        query_language="en",
        provider="crossref",
        lane="refresh",
        request_signature_value=request_signature(page_size=1),
        request_cursor="*",
        next_cursor=None,
        provider_exhausted=True,
        candidates=[PaperCandidate(title="Slow", year=2026, doi="10.9200/slow-lease")],
        state="cursor_committed", relevance_profile_hash=PROFILE_HASH,
    ))
    page_path = journal.write_page(page)
    runtime = DiscoveryBatchRuntime.create(
        journal=journal,
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        needs_staging=True,
        active_relevance_profiles=ActiveRelevanceProfiles.build({kid: PROFILE_HASH}),
    )
    real_datetime = pending_queue_module.datetime

    class FutureDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + timedelta(seconds=51)

    monkeypatch.setattr(pending_queue_module, "datetime", FutureDatetime)

    report = drain_pending_candidates(
        journal=journal,
        keyword_ids=[kid],
        candidate_budget=1,
        stage_to_paper_raw=True,
        apply=True,
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        locks_dir=tmp_path / "locks",
        exports_dir=tmp_path / "exports",
        worker_id="worker",
        lease_seconds=100,
        runtime=runtime,
    )

    assert report.staged == 1
    assert runtime.metrics.candidate_lease_renewals == 1
    assert runtime.metrics.page_fsyncs == 3  # claim, threshold renewal, commit
    assert journal.read(page_path)["state"] == "drained"
