"""A live Registry snapshot follows formal-to-raw lifecycle rollback."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.discovery.staging_context import DiscoveryStagingContext
from src.library.paper_number_ledger import PaperNumberLedger
from src.services.network_metadata_staging import stage_network_metadata_records
from tests.factories.discovery_factory import create_discovery_candidate
from tests.factories.paper_raw_factory import create_active_formal_workspace


pytestmark = pytest.mark.integration


def test_active_rollback_replaces_formal_record_and_all_index_refs(tmp_path: Path):
    doi = "10.1000/active-rollback"
    formal = create_active_formal_workspace(tmp_path, doi=doi)
    ledger = PaperNumberLedger(tmp_path / "ledger.json")
    number = next(iter(ledger.load()["items"]))
    context = DiscoveryStagingContext.create(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json")
    original = context.registry.records_by_number[number]
    assert original.scope == "papers"
    assert original.workspace_path == formal
    before_max = ledger.load()["max_number"]

    raw = tmp_path / "paper_raw" / number
    formal.rename(raw)
    ledger.rollback_active_to_metadata_staged(number, raw)
    report = stage_network_metadata_records(
        [create_discovery_candidate(doi=doi)],
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json", apply=True, transaction=context.transaction)

    item = report["items"][0]
    assert item["status"] == "staged"
    assert item["actual_allocated"] is False
    assert item["reused_existing"] is True
    assert Path(item["folder"]) == raw
    assert raw.is_dir()
    assert ledger.load()["max_number"] == before_max
    replacement = context.transaction.registry_snapshot.records_by_number[number]
    assert replacement.scope == "paper_raw"
    assert replacement.workspace_path == raw
    assert number not in context.transaction.registry_snapshot.indexed_formal_numbers
    doi_refs = context.transaction.registry_snapshot.doi_index.lookup_doi(doi)
    assert doi_refs and all(Path(ref.folder) == raw for ref in doi_refs)
    identity_refs = context.transaction.registry_snapshot.workspace_id_index.lookup_by_doi(doi)
    assert identity_refs and all(Path(ref.workspace_path) == raw for ref in identity_refs)
    assert all(Path(ref.folder) != formal for ref in doi_refs)
    assert all(Path(ref.workspace_path) != formal for ref in identity_refs)

