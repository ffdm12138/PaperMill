"""Safety regressions for discovery audit and explicit repair commands."""
from __future__ import annotations

import json
from pathlib import Path

from scripts.audit_discovery_workspace_registry import audit
from scripts.repair_discovery_workspaces import run as run_repair
from src.library.paper_number_ledger import PaperNumberLedger
from tests.factories.paper_raw_factory import create_network_metadata_workspaces_bulk


def _replace_workspace_doi(folder: Path, old: str, new: str) -> None:
    for path in folder.rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        if old in text:
            path.write_text(text.replace(old, new), encoding="utf-8")


def _mark_reserved(root: Path, number: str) -> None:
    folder = root / "paper_raw" / number
    ledger = PaperNumberLedger(root / "ledger.json")
    data = ledger.load()
    data["items"][number]["state"] = "reserved"
    ledger.save(data)
    PaperNumberLedger.write_marker(folder, number, state="reserved")


def test_audit_keeps_healthy_conflicts_when_an_unrelated_workspace_is_corrupt(
    tmp_path: Path,
):
    create_network_metadata_workspaces_bulk(tmp_path, count=3)
    first = "0000000000000001"
    second = "0000000000000002"
    corrupt = "0000000000000003"
    _replace_workspace_doi(
        tmp_path / "paper_raw" / second,
        "10.7000/bench.2", "10.7000/bench.1",
    )
    (tmp_path / "paper_raw" / corrupt / f"{corrupt}.metadata.json").write_text(
        "{", encoding="utf-8",
    )

    report = audit(
        paper_raw=tmp_path / "paper_raw", papers=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
    )

    assert report["registry_complete"] is False
    assert report["conflict_analysis_complete"] is False
    assert report["partial_healthy_record_count"] == 2
    assert [entry["doi"] for entry in report["paper_raw_doi_conflicts"]] == [
        "10.7000/bench.1"
    ]
    refs = report["paper_raw_doi_conflicts"][0]["refs"]
    assert {ref["paper_number"] for ref in refs} == {first, second}


def test_ready_reserved_duplicate_is_quarantined_instead_of_promoted(tmp_path: Path):
    create_network_metadata_workspaces_bulk(tmp_path, count=2)
    primary = "0000000000000001"
    duplicate = "0000000000000002"
    duplicate_folder = tmp_path / "paper_raw" / duplicate
    _replace_workspace_doi(
        duplicate_folder, "10.7000/bench.2", "10.7000/bench.1",
    )
    _mark_reserved(tmp_path, duplicate)
    ledger = PaperNumberLedger(tmp_path / "ledger.json")
    before = (tmp_path / "ledger.json").read_bytes()

    planned = run_repair(
        paper_raw=tmp_path / "paper_raw", papers=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json", apply=False, limit=100,
        paper_number=duplicate,
    )
    row = planned["items"][0]
    assert row["action"] == "quarantine_duplicate"
    assert row["duplicate_of"] == primary
    assert row["applied"] is False
    assert (tmp_path / "ledger.json").read_bytes() == before
    assert not (tmp_path / "paper_raw" / ".paper_raw_write.lock").exists()
    assert ledger.load()["items"][duplicate]["state"] == "reserved"

    applied = run_repair(
        paper_raw=tmp_path / "paper_raw", papers=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json", apply=True, limit=100,
        paper_number=duplicate,
    )
    assert applied["promoted"] == 0
    assert applied["quarantined"] == 1
    item = ledger.load()["items"][duplicate]
    assert item["state"] == "abandoned"
    assert item["quarantine_reason"] == "duplicate_workspace"
    assert item["quarantined_duplicate_of"] == primary
    assert item["quarantine_path"] == str(duplicate_folder)
    marker = json.loads(
        (duplicate_folder / f"{duplicate}.paper.number").read_text(encoding="utf-8")
    )
    assert marker["state"] == "abandoned"


def test_corrupt_reserved_workspace_is_reported_and_never_promoted(tmp_path: Path):
    create_network_metadata_workspaces_bulk(tmp_path, count=1)
    number = "0000000000000001"
    _mark_reserved(tmp_path, number)
    (tmp_path / "paper_raw" / number / f"{number}.metadata.json").write_text(
        "{", encoding="utf-8",
    )

    report = run_repair(
        paper_raw=tmp_path / "paper_raw", papers=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json", apply=True, limit=100,
        paper_number=number,
    )

    assert report["registry_complete"] is False
    assert report["promoted"] == 0
    assert report["quarantined"] == 0
    assert report["items"][0]["action"] == "repair_required_corrupt_json"
    assert PaperNumberLedger(tmp_path / "ledger.json").load()["items"][number][
        "state"
    ] == "reserved"


def test_unrelated_corrupt_workspace_does_not_block_selected_healthy_repair(
    tmp_path: Path,
):
    create_network_metadata_workspaces_bulk(tmp_path, count=2)
    healthy = "0000000000000001"
    corrupt = "0000000000000002"
    _mark_reserved(tmp_path, healthy)
    (tmp_path / "paper_raw" / corrupt / f"{corrupt}.metadata.json").write_text(
        "{", encoding="utf-8")

    report = run_repair(
        paper_raw=tmp_path / "paper_raw", papers=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json", apply=True, limit=1,
        paper_number=healthy)

    assert report["registry_complete"] is False
    assert report["registry_usable_for_repair"] is True
    assert report["promoted"] == 1
    assert report["items"][0]["action"] == "promote_metadata_staged"
    assert report["items"][0]["applied"] is True
    assert PaperNumberLedger(tmp_path / "ledger.json").load()["items"][healthy][
        "state"] == "metadata_staged"
