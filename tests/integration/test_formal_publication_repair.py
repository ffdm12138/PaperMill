"""Identity-only formal publication migration safety tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.repair_formal_publications import run as run_formal_repair
from src.discovery.workspace_registry import build_workspace_registry
from src.ingest.commit import commit_paper_raw
from src.ingest.formalization import write_formalization_plan
from src.library.paper_number_ledger import PaperNumberLedger
from tests.integration.test_frozen_v32_transaction_pipeline import _workspace


pytestmark = pytest.mark.integration


def _commit_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, str]:
    workspace, papers, ledger_path, catalog_root = _workspace(tmp_path)
    write_formalization_plan(workspace, papers_dir=papers)
    result = commit_paper_raw(
        workspace, paper_raw_root=tmp_path / "paper_raw", papers_dir=papers,
        ledger_path=ledger_path, catalog_root=catalog_root,
        transactions_dir=tmp_path / "transactions",
    )
    return papers, ledger_path, tmp_path / "transactions", catalog_root, result["paper_name"]


def test_identity_only_formal_repair_updates_marker_hash_and_state(tmp_path: Path):
    papers, ledger_path, transactions, _, paper_name = _commit_fixture(tmp_path)
    formal = papers / paper_name
    number = "0000000000000001"
    marker_path = formal / f"{number}.paper.number"
    manifest_path = formal / f"{paper_name}.asset_manifest.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["planned_paper_id"] = marker.pop("planned_paper_name")
    marker_path.write_text(json.dumps(marker, ensure_ascii=False), encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["paper_id"] = manifest.pop("paper_name")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    report = run_formal_repair(
        papers=papers, ledger_path=ledger_path, transactions=transactions,
        apply=False, limit=10,
    )
    assert report["rows"][0]["action"] == "repair_identity"
    applied = run_formal_repair(
        papers=papers, ledger_path=ledger_path, transactions=transactions,
        apply=True, limit=10,
    )
    assert applied["applied"] == 1
    assert json.loads(marker_path.read_text(encoding="utf-8"))["planned_paper_name"] == paper_name
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["paper_name"] == paper_name
    built = build_workspace_registry(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=papers,
        ledger=PaperNumberLedger(ledger_path),
    )
    assert built.complete is True


def test_formal_repair_refuses_existing_closure_drift(tmp_path: Path):
    papers, ledger_path, transactions, _, paper_name = _commit_fixture(tmp_path)
    formal = papers / paper_name
    catalog_path = formal / f"{paper_name}.catalog.json"
    before = catalog_path.read_bytes()
    catalog_path.write_bytes(before + b" ")
    report = run_formal_repair(
        papers=papers, ledger_path=ledger_path, transactions=transactions,
        apply=True, limit=10,
    )
    assert report["blocked"] is True
    assert report["rows"][0]["action"] == "rollback_recommit_required"
    assert catalog_path.read_bytes() == before + b" "
