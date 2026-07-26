from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.discovery.stage_transaction import DiscoveryStageTransaction
from src.discovery.workspace_registry import build_workspace_registry
from src.ingest.paper_raw import PaperRawAllocator
from src.library.paper_number_ledger import PaperNumberLedger
from src.staging.network_metadata_staging import stage_network_metadata_records
from tests.factories.discovery_factory import create_discovery_candidate
from tests.factories.paper_raw_factory import create_network_metadata_workspace


pytestmark = pytest.mark.integration


def _issue_codes(issues) -> set[str]:
    return {
        str(getattr(getattr(issue, "code", None), "value", getattr(issue, "category", "")))
        for issue in issues
    }


@pytest.mark.parametrize(
    "artifact",
    ("metadata", "source_record", "receipt", "stage_manifest", "import_status", "marker"),
)
def test_metadata_staged_missing_evidence_fails_closed_before_allocation(
    tmp_path: Path, artifact: str,
):
    paper_raw = tmp_path / "paper_raw"
    papers = tmp_path / "papers"
    ledger_path = tmp_path / "ledger.json"
    empty = build_workspace_registry(
        paper_raw_dir=paper_raw, papers_dir=papers, ledger=PaperNumberLedger(ledger_path))
    assert empty.complete and empty.registry is not None

    workspace = create_network_metadata_workspace(tmp_path, doi="10.9100/existing")
    targets = {
        "metadata": next(workspace.glob("*.metadata.json")),
        "source_record": next((workspace / "source_records").glob("metadata_source.*.json")),
        "receipt": next(workspace.glob("*.discovery_receipt.json")),
        "stage_manifest": workspace / "stage_manifest.json",
        "import_status": workspace / ".import_status.json",
        "marker": next(workspace.glob("*.paper.number")),
    }
    targets[artifact].unlink()

    ledger_before = PaperNumberLedger(ledger_path).load()
    dirs_before = {path.name for path in paper_raw.iterdir() if path.is_dir()}
    markers_before = tuple(sorted(path.relative_to(paper_raw) for path in paper_raw.rglob("*.paper.number")))
    receipts_before = tuple(sorted(path.relative_to(paper_raw) for path in paper_raw.rglob("*.discovery_receipt.json")))

    rebuilt = build_workspace_registry(
        paper_raw_dir=paper_raw, papers_dir=papers, ledger=PaperNumberLedger(ledger_path))
    assert not rebuilt.complete
    assert rebuilt.registry is not None
    assert workspace.name not in rebuilt.registry.records_by_number
    assert "metadata_staged_workspace_incomplete" in _issue_codes(rebuilt.issues)

    transaction = DiscoveryStageTransaction(
        paper_raw_dir=paper_raw, papers_dir=papers, ledger_path=ledger_path,
        registry_snapshot=empty.registry,
    )
    with patch.object(PaperRawAllocator, "_allocate_workspace_unlocked", autospec=True) as allocate:
        report = stage_network_metadata_records(
            [create_discovery_candidate(doi="10.9100/new")],
            paper_raw_dir=paper_raw, papers_dir=papers, ledger_path=ledger_path,
            apply=True, transaction=transaction,
        )

    assert report["items"][0]["status"] == "repair_required"
    allocate.assert_not_called()
    ledger_after = PaperNumberLedger(ledger_path).load()
    assert ledger_after["max_number"] == ledger_before["max_number"]
    assert {path.name for path in paper_raw.iterdir() if path.is_dir()} == dirs_before
    assert tuple(sorted(path.relative_to(paper_raw) for path in paper_raw.rglob("*.paper.number"))) == markers_before
    assert tuple(sorted(path.relative_to(paper_raw) for path in paper_raw.rglob("*.discovery_receipt.json"))) == receipts_before


def test_reserved_missing_metadata_remains_valid_unsettled(tmp_path: Path):
    number, workspace = PaperNumberLedger(tmp_path / "ledger.json").reserve_next_for_paper_raw_workspace(
        tmp_path / "paper_raw")

    result = build_workspace_registry(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger=PaperNumberLedger(tmp_path / "ledger.json"),
    )

    assert result.complete
    assert result.registry is not None
    assert number in result.registry.repair_backlog_numbers
    assert "metadata_staged_workspace_incomplete" not in _issue_codes(result.issues)
    assert workspace.is_dir()
