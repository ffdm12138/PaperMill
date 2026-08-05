"""Plan-phase unit tests for scripts/rematch_paper_raw_pdf_identity.py.

The migration tool's transactional apply paths are covered by
``tests/integration/test_pdf_identity_migration_transaction.py``; this file
pins the plan-phase semantics: frozen workspaces ARE re-planned, the plan
pins freeze targets, and the old receipts are snapshotted.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

from src.metadata.freeze import freeze_metadata
from src.metadata.pdf_identity import extract_pdf_identity_evidence
from src.metadata.pdf_match import build_match_receipt, write_match_receipt
from src.metadata.schema import empty_metadata

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PN = "0000000000000101"
DOI = "10.5194/acp-26-9643-2026"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "rematch_paper_raw_pdf_identity",
        _REPO_ROOT / "scripts" / "rematch_paper_raw_pdf_identity.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fitz_pdf(path: Path) -> None:
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Rematch Paper")
    page.insert_text((72, 100), f"doi:{DOI}")
    page.insert_text((72, 114), "Jane Smith")
    doc.save(str(path), deflate=True)
    doc.close()


def _workspace(root: Path) -> Path:
    folder = root / PN
    folder.mkdir(parents=True)
    metadata = empty_metadata(PN)
    metadata["title"]["original"] = "Rematch Paper"
    metadata["year"] = 2024
    metadata["authors"] = [{"full_name": "Jane Smith", "family": "Smith", "given": "Jane", "orcid": "", "affiliation": ""}]
    metadata["first_author"] = {"family": "Smith", "display": "Jane Smith"}
    metadata["container"]["journal"] = "Test Journal"
    metadata["identifiers"]["doi"] = DOI
    metadata["source"].update({"provider": "fixture", "raw_record_path": "source_records/metadata_source.fixture.json"})
    (folder / f"{PN}.metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (folder / f"{PN}.paper.number").write_text(
        json.dumps({"paper_number": PN, "folder_name": PN, "state": "active"}),
        encoding="utf-8",
    )
    (folder / "source_records").mkdir()
    (folder / metadata["source"]["raw_record_path"]).write_text(json.dumps({"doi": DOI}), encoding="utf-8")
    return folder


def _run_plan(module, root: Path, plan_file: Path) -> str:
    saved = sys.argv
    sys.argv = ["rematch_paper_raw_pdf_identity.py", "--plan", "--all",
                "--paper-raw-dir", str(root), "--plan-file", str(plan_file)]
    try:
        module.main()
    finally:
        sys.argv = saved
    return json.loads(plan_file.read_text(encoding="utf-8"))


def test_plan_rebuilds_mismatch_and_includes_frozen(tmp_path: Path) -> None:
    root = tmp_path / "paper_raw"
    folder = _workspace(root)
    _fitz_pdf(folder / f"{PN}.pdf")
    # Pre-migration state: a stale v1-style mismatch receipt.
    (folder / f"{PN}.metadata_match.json").write_text(
        json.dumps({"schema_version": "1.0", "match_status": "mismatch"}),
        encoding="utf-8",
    )

    module = _load_script()
    plan_file = tmp_path / "plan.json"
    plan = _run_plan(module, root, plan_file)
    entry = plan["papers"][PN]
    # The plan rebuilds the decision under v2 -> matched and pins the
    # freeze target; the old receipt hash is snapshotted for drift checks.
    assert entry["receipt"]["schema_version"] == "2.0"
    assert entry["receipt"]["match_status"] == "matched"
    assert entry["old_receipt_sha256"]
    assert entry["freeze_eligible"] is True
    assert entry["target_revision"] == 1
    assert entry["target_freeze_sha256"]
    assert entry["baseline"]["old_match_status"] == "mismatch"


def test_plan_includes_frozen_workspaces_and_pins_revision_plus_one(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper_raw"
    folder = _workspace(root)
    _fitz_pdf(folder / f"{PN}.pdf")
    # Build a genuine frozen workspace, then downgrade the receipt to the
    # pre-migration v1 shape.
    metadata = json.loads((folder / f"{PN}.metadata.json").read_text(encoding="utf-8"))
    evidence = extract_pdf_identity_evidence(pdf_path=folder / f"{PN}.pdf")
    receipt = build_match_receipt(folder, PN, metadata, evidence)
    write_match_receipt(folder, receipt)
    frozen = freeze_metadata(folder, PN)
    assert frozen["revision"] == 1
    receipt["schema_version"] = "1.0"
    write_match_receipt(folder, receipt)

    module = _load_script()
    plan_file = tmp_path / "plan.json"
    plan = _run_plan(module, root, plan_file)
    entry = plan["papers"][PN]
    # Frozen workspaces are NEVER skipped by the plan: they are re-planned
    # with revision = old + 1 and a pinned frozen_at.
    assert entry["old_freeze_existed"] is True
    assert entry["target_revision"] == 2
    assert entry["target_frozen_at"]
    assert entry["freeze_eligible"] is True
    assert entry["baseline"]["freeze_sha256"]


def test_plan_records_workspace_inventory_and_hash(tmp_path: Path) -> None:
    root = tmp_path / "paper_raw"
    folder = _workspace(root)
    _fitz_pdf(folder / f"{PN}.pdf")
    module = _load_script()
    plan_file = tmp_path / "plan.json"
    plan = _run_plan(module, root, plan_file)
    assert plan["workspace_inventory_hash"]
    assert plan["coverage"] == {"total": 1, "planned": 1, "complete": True}
    # The plan hash covers everything except its own field.
    assert plan["plan_content_hash"]
    assert "plan_file_sha256" not in plan
