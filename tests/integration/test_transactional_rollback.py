"""Integration tests for public rollback and its complete crash matrix."""
from __future__ import annotations

from pathlib import Path

import pytest
import json
import uuid

from src.file_fingerprint import compute_sha256
from src.ingest.commit import commit_paper_raw
from src.ingest.formalization import write_formalization_plan
from src.ingest.rollback import rollback_formal_papers
from src.library.paper_number_ledger import PaperNumberLedger
from tests.integration.test_frozen_v32_transaction_pipeline import NUMBER, _workspace


def _committed(tmp_path: Path):
    workspace, papers, ledger_path, catalog_root = _workspace(tmp_path)
    metadata_hash = compute_sha256(workspace.metadata)
    catalog_hash = compute_sha256(workspace.catalog)
    write_formalization_plan(workspace, papers_dir=papers)
    result = commit_paper_raw(
        workspace, paper_raw_root=tmp_path / "paper_raw", papers_dir=papers, ledger_path=ledger_path,
        catalog_root=catalog_root, transactions_dir=tmp_path / "transactions",
    )
    return result, papers, ledger_path, catalog_root, metadata_hash, catalog_hash


def _rollback(tmp_path: Path):
    result, papers, ledger_path, catalog_root, metadata_hash, catalog_hash = _committed(tmp_path)
    paper_raw_root = tmp_path / "paper_raw"
    transaction_root = tmp_path / "transactions"
    number = rollback_formal_papers(
        papers_dir=papers,
        paper_raw_root=paper_raw_root,
        transaction_root=transaction_root,
        ledger_path=ledger_path,
        catalog_root=catalog_root,
        paper_number=result["paper_number"],
    )
    return number, result, papers, ledger_path, catalog_root, metadata_hash, catalog_hash


def test_commit_rollback_roundtrip_preserves_frozen_bytes_and_indexes(tmp_path: Path):
    number, result, papers, ledger_path, catalog_root, metadata_hash, catalog_hash = _rollback(tmp_path)
    assert number == result["paper_number"]
    raw = tmp_path / "paper_raw" / NUMBER
    assert compute_sha256(raw / f"{NUMBER}.metadata.json") == metadata_hash
    assert compute_sha256(raw / f"{NUMBER}.catalog.json") == catalog_hash
    assert not (papers / result["paper_id"]).exists()
    assert not list((catalog_root / "all").iterdir())


def test_rollback_requires_paper_number_or_paper_id(tmp_path: Path):
    """Without paper_number or paper_id, ``rollback_formal_papers`` raises."""
    with pytest.raises(ValueError, match="paper_number.*paper_id"):
        rollback_formal_papers(
            papers_dir=tmp_path / "papers",
            paper_raw_root=tmp_path / "paper_raw",
            transaction_root=tmp_path / "transactions",
            ledger_path=tmp_path / "ledger.json",
            catalog_root=tmp_path / "catalog",
        )


@pytest.mark.parametrize("crash_phase", [
    "prepared",
    "formal_quarantined",
    "raw_installed",
    "ledger_reserved",
    "category_links_removed",
    "quarantine_removed",
    "before_completed",
])
def test_public_rollback_recovers_every_crash_boundary(tmp_path: Path, crash_phase: str):
    result, papers, ledger_path, catalog_root, _, _ = _committed(tmp_path)
    transaction_root = tmp_path / "transactions"
    paper_raw_root = tmp_path / "paper_raw"
    raised = False

    def crash_once(phase: str) -> None:
        nonlocal raised
        if phase == crash_phase and not raised:
            raised = True
            raise RuntimeError(f"injected crash at {phase}")

    with pytest.raises(RuntimeError, match="injected crash"):
        rollback_formal_papers(
            papers_dir=papers,
            paper_raw_root=paper_raw_root,
            transaction_root=transaction_root,
            ledger_path=ledger_path,
            catalog_root=catalog_root,
            paper_number=NUMBER,
            fault_injector=crash_once,
        )

    journal_files = list((transaction_root / "rollback").glob("*.json"))
    assert len(journal_files) == 1
    transaction_id = journal_files[0].stem
    assert rollback_formal_papers(
        papers_dir=papers,
        paper_raw_root=paper_raw_root,
        transaction_root=transaction_root,
        ledger_path=ledger_path,
        catalog_root=catalog_root,
        paper_number=NUMBER,
    ) == NUMBER

    completed = list((transaction_root / "rollback" / "completed").glob("*.json"))
    assert [path.stem for path in completed] == [transaction_id]
    assert not list((transaction_root / "rollback").glob("*.json"))
    journal = __import__("json").loads(completed[0].read_text(encoding="utf-8"))
    assert journal["phase"] == "completed"
    assert not (papers / result["paper_id"]).exists()
    assert (paper_raw_root / NUMBER).is_dir()
    assert not list(papers.glob(".*.rollback_quarantine_*"))
    assert not list(paper_raw_root.glob(".rollback_*"))
    assert ((PaperNumberLedger(ledger_path).load().get("items") or {})[NUMBER]["state"] == "reserved")
    assert not list((catalog_root / "all").iterdir())


def test_rollback_fails_closed_on_active_commit_journal(tmp_path: Path):
    workspace, papers, ledger_path, catalog_root = _workspace(tmp_path)
    write_formalization_plan(workspace, papers_dir=papers)

    def crash_after_commit_journal(phase: str) -> None:
        if phase == "prepared":
            raise RuntimeError("commit interrupted")

    with pytest.raises(RuntimeError, match="commit interrupted"):
        commit_paper_raw(
            workspace,
            paper_raw_root=tmp_path / "paper_raw",
            papers_dir=papers,
            ledger_path=ledger_path,
            catalog_root=catalog_root,
            transactions_dir=tmp_path / "transactions",
            fault_injector=crash_after_commit_journal,
        )
    with pytest.raises(RuntimeError, match="active_commit_transaction"):
        rollback_formal_papers(
            papers_dir=papers,
            paper_raw_root=tmp_path / "paper_raw",
            transaction_root=tmp_path / "transactions",
            ledger_path=ledger_path,
            catalog_root=catalog_root,
            paper_number=NUMBER,
        )


def test_rollback_rejects_ambiguous_active_journals(tmp_path: Path):
    result, papers, ledger_path, catalog_root, _, _ = _committed(tmp_path)
    transaction_root = tmp_path / "transactions"

    def crash_after_journal(phase: str) -> None:
        if phase == "prepared":
            raise RuntimeError("rollback interrupted")

    with pytest.raises(RuntimeError, match="rollback interrupted"):
        rollback_formal_papers(
            papers_dir=papers,
            paper_raw_root=tmp_path / "paper_raw",
            transaction_root=transaction_root,
            ledger_path=ledger_path,
            catalog_root=catalog_root,
            paper_number=NUMBER,
            fault_injector=crash_after_journal,
        )
    first_path = next((transaction_root / "rollback").glob("*.json"))
    duplicate = json.loads(first_path.read_text(encoding="utf-8"))
    second_id = str(uuid.uuid4())
    duplicate["transaction_id"] = second_id
    duplicate["staging_path"] = str(
        (tmp_path / "paper_raw" / f".rollback_{NUMBER}_{second_id}").resolve()
    )
    duplicate["formal_quarantine"] = str(
        (papers / f".{result['paper_id']}.rollback_quarantine_{second_id}").resolve()
    )
    (transaction_root / "rollback" / f"{second_id}.json").write_text(
        json.dumps(duplicate), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="ambiguous_active_transaction"):
        rollback_formal_papers(
            papers_dir=papers,
            paper_raw_root=tmp_path / "paper_raw",
            transaction_root=transaction_root,
            ledger_path=ledger_path,
            catalog_root=catalog_root,
            paper_number=NUMBER,
        )
