"""Registry identity projection and copy-on-write refresh contracts."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.discovery.stage_transaction import StageTransactionConfigurationError
from src.discovery.staging_context import DiscoveryStagingContext
from src.discovery.workspace_registry import build_workspace_registry, refresh_registry_under_write_lock
from src.library.paper_number_ledger import PaperNumberLedger
from tests.factories.discovery_factory import create_discovery_candidate
from tests.factories.paper_raw_factory import create_network_metadata_workspace


def _build(tmp_path: Path):
    ledger = PaperNumberLedger(tmp_path / "ledger.json")
    result = build_workspace_registry(paper_raw_dir=tmp_path / "paper_raw",
                                      papers_dir=tmp_path / "papers", ledger=ledger)
    assert result.complete and result.registry is not None, result.issues
    return ledger, result.registry


def test_identity_and_doi_come_from_same_scan_record(tmp_path: Path):
    folder = create_network_metadata_workspace(tmp_path, doi="10.1000/same-record")
    _, snapshot = _build(tmp_path)
    record = snapshot.records_by_number[folder.name]
    assert record.doi_refs[0].normalized_doi == "10.1000/same-record"
    assert record.identity_refs[0].normalized_doi == "10.1000/same-record"
    assert snapshot.workspace_id_index.lookup(
        candidate_id="candidate-1", page_id="page-1", keyword_id="keyword-1",
        provider="crossref", normalized_doi="10.1000/same-record",
    )[0].paper_number == folder.name


def test_failed_refresh_does_not_mutate_live_snapshot(tmp_path: Path):
    first = create_network_metadata_workspace(tmp_path, doi="10.1000/a", candidate_id="a")
    ledger, snapshot = _build(tmp_path)
    before_dois = tuple(snapshot.doi_index.lookup_doi("10.1000/a"))
    before_refs = snapshot.workspace_id_index.refs
    before_max = snapshot.observed_paper_raw_max
    before_unsettled = snapshot.unsettled_paper_raw_numbers

    second = create_network_metadata_workspace(tmp_path, doi="10.1000/b", candidate_id="b")
    (second / f"{second.name}.discovery_receipt.json").write_text("{bad", encoding="utf-8")
    refreshed = refresh_registry_under_write_lock(
        snapshot, paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_view=ledger.load())
    assert refreshed.status == "repair_required"
    assert refreshed.snapshot is None
    assert tuple(snapshot.doi_index.lookup_doi("10.1000/a")) == before_dois
    assert snapshot.workspace_id_index.refs == before_refs
    assert snapshot.observed_paper_raw_max == before_max
    assert snapshot.unsettled_paper_raw_numbers == before_unsettled
    assert not snapshot.doi_index.lookup_doi("10.1000/b")


def test_unchanged_refresh_reuses_frozen_snapshot(tmp_path: Path):
    create_network_metadata_workspace(tmp_path, doi="10.1000/stable", candidate_id="stable")
    ledger, snapshot = _build(tmp_path)

    refreshed = refresh_registry_under_write_lock(
        snapshot, paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_view=ledger.load())

    assert refreshed.status == "ok"
    assert refreshed.snapshot is snapshot
    assert refreshed.scanned_numbers == ()


def test_each_json_artifact_read_once(tmp_path: Path, monkeypatch):
    folder = create_network_metadata_workspace(tmp_path, doi="10.1000/reads")
    counts: dict[Path, int] = {}
    original = Path.read_text

    def counted(path: Path, *args, **kwargs):
        if folder in path.parents and path.suffix in {".json", ".number"}:
            counts[path] = counts.get(path, 0) + 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted)
    _build(tmp_path)
    assert counts
    assert max(counts.values()) <= 1, counts


def test_regressed_ledger_max_fails_before_discovery_allocation(tmp_path: Path):
    ledger = PaperNumberLedger(tmp_path / "ledger.json")
    number = "0000000000000001"
    data = ledger.empty_data()
    data["items"][number] = {
        "state": "abandoned", "folder_name": number, "folder_path": "",
        "abandoned_reason": "historical allocation",
    }
    ledger.save(data)
    before = ledger.path.read_bytes()

    built = build_workspace_registry(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger=ledger)
    assert not built.complete
    assert built.registry is None
    assert any(issue.detail == "max_number_below_item_floor" for issue in built.issues)

    with pytest.raises(StageTransactionConfigurationError, match="max_number_below_item_floor"):
        DiscoveryStagingContext.create(
            paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
            ledger_path=ledger.path)

    assert ledger.path.read_bytes() == before
    assert not list((tmp_path / "paper_raw").glob("[0-9]*"))
