"""Unsettled workspaces remain visible to registry incremental refresh."""
from pathlib import Path

from src.discovery.workspace_registry import refresh_registry_under_write_lock
from src.discovery.staging_context import DiscoveryStagingContext
from src.library.paper_number_ledger import PaperNumberLedger
from src.staging.network_metadata_staging import stage_network_metadata_records
from tests.factories.discovery_factory import create_discovery_candidate


def test_late_identity_reuses_reserved_number(tmp_path: Path):
    ledger = PaperNumberLedger(tmp_path / "ledger.json")
    number, _ = ledger.reserve_next_for_paper_raw_workspace(tmp_path / "paper_raw")
    context = DiscoveryStagingContext.create(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json", prepare_allocation=False)
    assert number in context.registry.repair_backlog_numbers
    from src.metadata.source_records import write_metadata_source_record
    write_metadata_source_record(tmp_path / "paper_raw" / number, "crossref", {
        "provider": "crossref", "record": {},
        "discovery_context": {"candidate_id": "candidate-1", "page_id": "page-1",
                              "keyword_id": "keyword-1", "provider": "crossref",
                              "normalized_doi": "10.1000/late"},
    })
    report = stage_network_metadata_records(
        [create_discovery_candidate(doi="10.1000/late")],
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json", apply=True, reuse_paper_number=number,
        transaction=context.transaction)
    assert report["items"][0]["paper_number"] == number


def test_corrupt_ledger_refresh_returns_no_snapshot(tmp_path: Path):
    context = DiscoveryStagingContext.create(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json", prepare_allocation=False)
    result = refresh_registry_under_write_lock(
        context.registry, paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers", ledger_view={"schema_version": "1.0"})
    assert result.status == "retryable_failure"
    assert result.snapshot is None
