"""Discovery reconciliation belongs exclusively to the stage transaction."""
from pathlib import Path

from src.discovery.staging_context import DiscoveryStagingContext
from src.staging.network_metadata_staging import stage_network_metadata_records
from tests.factories.discovery_factory import create_discovery_candidate
from tests.factories.paper_raw_factory import create_network_metadata_workspace


def test_complete_identity_is_reused_without_new_number(tmp_path: Path):
    first = create_network_metadata_workspace(tmp_path, doi="10.1234/reconcile", candidate_id="same")
    context = DiscoveryStagingContext.create(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json")
    report = stage_network_metadata_records(
        [create_discovery_candidate(doi="10.1234/reconcile", candidate_id="same")],
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json", apply=True, transaction=context.transaction)
    assert report["staged"] == 1
    assert report["items"][0]["paper_number"] == first.name
    assert len([p for p in (tmp_path / "paper_raw").iterdir() if p.is_dir()]) == 1


def test_conflicting_identity_fails_before_workspace_mutation(tmp_path: Path):
    first = create_network_metadata_workspace(tmp_path, doi="10.1234/conflict", candidate_id="one")
    before = {p.relative_to(first): p.read_bytes() for p in first.rglob("*") if p.is_file()}
    report = stage_network_metadata_records(
        [create_discovery_candidate(doi="10.1234/conflict", candidate_id="two")],
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json", apply=True, reuse_paper_number=first.name)
    assert report["failed"] == 1
    assert {p.relative_to(first): p.read_bytes() for p in first.rglob("*") if p.is_file()} == before
