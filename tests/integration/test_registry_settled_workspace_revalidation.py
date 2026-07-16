"""Matched settled workspaces are live facts, not permanent snapshot truth."""
from __future__ import annotations

from pathlib import Path
import json

import pytest

from src.discovery.keyword_notebook import keyword_id, query_identity
from src.discovery.models import PaperCandidate
from src.discovery.page_journal import INITIAL_CURSOR, PageJournalStore, request_signature
from src.discovery.batch_runtime import ActiveRelevanceProfiles, DiscoveryBatchRuntime
from src.discovery.pending_queue import drain_pending_candidates
from src.discovery.staging_context import DiscoveryStagingContext
from src.library.paper_number_ledger import PaperNumberLedger
from src.services.network_metadata_staging import stage_network_metadata_records
from tests.factories.discovery_factory import create_discovery_candidate
from tests.factories.paper_raw_factory import create_metadata_staged_network_workspace


pytestmark = pytest.mark.integration
PROFILE_HASH = "test-active-profile"


def _artifact(folder: Path, kind: str) -> Path:
    number = folder.name
    paths = {
        "metadata": folder / f"{number}.metadata.json",
        "receipt": folder / f"{number}.discovery_receipt.json",
        "stage_manifest": folder / "stage_manifest.json",
        "import_status": folder / ".import_status.json",
        "marker": folder / f"{number}.paper.number",
    }
    if kind == "source_record":
        return next((folder / "source_records").glob("*.json"))
    return paths[kind]


@pytest.mark.parametrize(
    "missing",
    ["metadata", "source_record", "receipt", "stage_manifest", "import_status", "marker"],
)
def test_matched_metadata_staged_workspace_is_revalidated_after_context_creation(
    tmp_path: Path, missing: str,
):
    doi = "10.1000/stale-settled"
    folder = create_metadata_staged_network_workspace(tmp_path, doi=doi)
    context = DiscoveryStagingContext.create(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json")
    assert context.registry.records_by_number[folder.name].readiness.ready is True
    before_max = PaperNumberLedger(tmp_path / "ledger.json").load()["max_number"]
    before_dirs = {path.name for path in (tmp_path / "paper_raw").iterdir() if path.is_dir()}

    _artifact(folder, missing).unlink()
    report = stage_network_metadata_records(
        [create_discovery_candidate(doi=doi)],
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json", apply=True, transaction=context.transaction)

    item = report["items"][0]
    assert item["status"] == "repair_required"
    assert item["actual_allocated"] is False
    assert item["reused_existing"] is False
    assert PaperNumberLedger(tmp_path / "ledger.json").load()["max_number"] == before_max
    assert {path.name for path in (tmp_path / "paper_raw").iterdir() if path.is_dir()} == before_dirs
    assert report["staged"] == 0


def test_inspect_doi_revalidates_matched_workspace(tmp_path: Path):
    doi = "10.1000/stale-inspection"
    folder = create_metadata_staged_network_workspace(tmp_path, doi=doi)
    context = DiscoveryStagingContext.create(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json")
    (folder / f"{folder.name}.metadata.json").unlink()

    result = context.transaction.inspect_doi(doi)

    assert result.status == "repair_required"
    assert result.error is not None


def test_hide_existing_defers_candidate_when_primary_is_damaged(
    tmp_path: Path,
):
    doi = "10.1000/stale-hide-existing"
    folder = create_metadata_staged_network_workspace(tmp_path, doi=doi)
    context = DiscoveryStagingContext.create(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json")
    (folder / f"{folder.name}.metadata.json").unlink()
    keyword_zh = "损坏主记录"
    store = PageJournalStore(tmp_path / "pages")
    page = store.make_page(
        page_id="page-stale", keyword_id=keyword_id(keyword_zh),
        keyword_zh=keyword_zh, query_id=query_identity("zh", keyword_zh),
        query=keyword_zh, query_language="zh", provider="openalex",
        lane="refresh", request_signature_value=request_signature(page_size=10),
        request_cursor=INITIAL_CURSOR, next_cursor=None, provider_exhausted=True,
        candidates=[PaperCandidate(title="Stale primary", doi=doi)],
        state="cursor_committed", relevance_profile_hash=PROFILE_HASH,
    )
    page["candidates"][0]["relevance"]["state"] = "passed"
    page_path = store.write_page(page)

    from src.discovery.batch_runtime import (
        DiscoveryBatchRuntime, DiscoveryPipelineMetrics, RepairBacklog)
    from src.discovery.page_journal import JournalDrainIndex
    runtime = DiscoveryBatchRuntime(
        context, JournalDrainIndex.build(
            store, active_profile_hashes={keyword_id(keyword_zh): PROFILE_HASH}),
        DiscoveryPipelineMetrics(),
        RepairBacklog(set(context.registry.repair_backlog_numbers)),
        ActiveRelevanceProfiles.build({keyword_id(keyword_zh): PROFILE_HASH}))
    report = drain_pending_candidates(
        journal=store, keyword_ids=[keyword_id(keyword_zh)], candidate_budget=1,
        stage_to_paper_raw=False, apply=False, hide_existing=True,
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json", locks_dir=tmp_path / "locks",
        exports_dir=tmp_path / "exports", worker_id="worker",
        runtime=runtime,
    )

    item = store.read(page_path)["candidates"][0]
    assert report.existing_duplicate == 0
    assert report.retryable_failures == 1
    assert item["status"] == "failed_retryable"
    assert item["last_deferred_reason"] == "repair_required"
    assert item.get("terminal_reason") != "existing_duplicate"


def test_batch_boundary_repair_probe_is_capped_and_does_not_clear_broken_members(
    tmp_path: Path,
):
    ledger = PaperNumberLedger(tmp_path / "ledger.json")
    for _ in range(25):
        ledger.reserve_next_for_paper_raw_workspace(tmp_path / "paper_raw")
    runtime = DiscoveryBatchRuntime.create(
        journal=PageJournalStore(tmp_path / "pages"),
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json", needs_staging=True,
        repair_probe_budget_per_batch=20,
        active_relevance_profiles=ActiveRelevanceProfiles.build({}),
    )

    assert runtime.metrics.staging.repair_backlog_probes == 20
    assert len(runtime.staging_context.registry.repair_backlog_numbers) == 25
    assert len(runtime.repair_backlog.numbers) == 25


def test_persisted_repair_probe_cursor_rotates_past_first_twenty(tmp_path: Path):
    ledger = PaperNumberLedger(tmp_path / "ledger.json")
    for _ in range(25):
        ledger.reserve_next_for_paper_raw_workspace(tmp_path / "paper_raw")
    kwargs = dict(
        journal=PageJournalStore(tmp_path / "pages"),
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json", needs_staging=True,
        repair_probe_budget_per_batch=20, persist_repair_cursor=True,
        active_relevance_profiles=ActiveRelevanceProfiles.build({}),
    )
    DiscoveryBatchRuntime.create(**kwargs)
    cursor_path = tmp_path / "paper_raw" / ".repair_probe_cursor.json"
    assert json.loads(cursor_path.read_text(encoding="utf-8"))[
        "last_paper_number"] == "0000000000000020"

    second = DiscoveryBatchRuntime.create(**kwargs)
    assert second.metrics.staging.repair_backlog_probes == 20
    assert json.loads(cursor_path.read_text(encoding="utf-8"))[
        "last_paper_number"] == "0000000000000015"
