"""Corrupt registry facts fail closed before allocation."""
from pathlib import Path

from src.services.network_metadata_staging import stage_network_metadata_records
from tests.factories.paper_raw_factory import create_network_metadata_workspace


def _files(root: Path) -> set[Path]:
    return {path.relative_to(root) for path in root.rglob("*") if path.is_file()} if root.exists() else set()


def test_corrupt_workspace_causes_zero_allocation(tmp_path: Path):
    folder = create_network_metadata_workspace(tmp_path, doi="10.1000/good")
    (folder / f"{folder.name}.metadata.json").write_text("{broken", encoding="utf-8")
    before = _files(tmp_path / "paper_raw")
    report = stage_network_metadata_records(
        [{"title": "New", "year": 2026, "doi": "10.1000/new"}],
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json", apply=True)
    assert report["items"][0]["status"] == "repair_required"
    assert _files(tmp_path / "paper_raw") == before


def test_corrupt_ledger_causes_zero_allocation(tmp_path: Path):
    create_network_metadata_workspace(tmp_path, doi="10.1000/good")
    (tmp_path / "ledger.json").write_text("{broken", encoding="utf-8")
    before = _files(tmp_path / "paper_raw")
    report = stage_network_metadata_records(
        [{"title": "New", "year": 2026, "doi": "10.1000/new"}],
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json", apply=True)
    assert report["items"][0]["status"] == "repair_required"
    assert _files(tmp_path / "paper_raw") == before
