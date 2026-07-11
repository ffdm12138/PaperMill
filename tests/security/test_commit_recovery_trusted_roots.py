from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from src.ingest.commit_recovery import reconcile_commits
from src.services.transaction_paths import TransactionPathError
from src.ingest.commit import CommitRecoveryCorruptionError, commit_paper_raw
from src.ingest.formalization import write_formalization_plan
from tests.integration.test_frozen_v32_transaction_pipeline import _workspace


@pytest.mark.parametrize("apply", [False, True])
def test_recovery_rejects_external_numeric_source_without_touching_sentinel(
    tmp_path: Path, apply: bool
) -> None:
    trusted = tmp_path / "trusted"
    paper_raw = trusted / "paper_raw"
    papers = trusted / "papers"
    transactions = trusted / "transactions"
    for path in (paper_raw, papers, transactions / "commit"):
        path.mkdir(parents=True)
    number = "1234567890123456"
    victim = tmp_path / "unrelated" / number
    victim.mkdir(parents=True)
    sentinel = victim / "KEEP.txt"
    sentinel.write_text("keep", encoding="utf-8")
    before = (sentinel.stat().st_mtime_ns, sentinel.stat().st_ino, sentinel.read_bytes())
    tx_id = str(uuid4())
    journal = {
        "schema_version": "1.0",
        "transaction_id": tx_id,
        "paper_number": number,
        "paper_id": "2024_Smith_safe",
        "source_workspace": str(victim),
        "staging_path": str(papers / f".2024_Smith_safe.staging_{tx_id}"),
        "final_path": str(papers / "2024_Smith_safe"),
        "phase": "prepared",
    }
    (transactions / "commit" / f"{tx_id}.json").write_text(
        json.dumps(journal), encoding="utf-8"
    )
    with pytest.raises(TransactionPathError):
        reconcile_commits(
            transactions_dir=transactions,
            paper_raw_root=paper_raw,
            papers_dir=papers,
            ledger_path=trusted / "ledger.json",
            catalog_root=trusted / "catalog",
            apply=apply,
        )
    assert (sentinel.stat().st_mtime_ns, sentinel.stat().st_ino, sentinel.read_bytes()) == before
    assert list(paper_raw.iterdir()) == []


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
@pytest.mark.parametrize("apply", [False, True])
def test_committed_state_damage_preserves_source_sentinel(
    tmp_path: Path, damage: str, apply: bool
) -> None:
    workspace, papers, ledger_path, catalog_root = _workspace(tmp_path)
    sentinel = workspace.root / "KEEP.txt"
    sentinel.write_text("keep", encoding="utf-8")
    write_formalization_plan(workspace, papers_dir=papers)

    def stop_after_category_request(phase: str) -> None:
        if phase == "category_reconcile_requested":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        commit_paper_raw(
            workspace,
            paper_raw_root=tmp_path / "paper_raw",
            papers_dir=papers,
            ledger_path=ledger_path,
            catalog_root=catalog_root,
            transactions_dir=tmp_path / "transactions",
            fault_injector=stop_after_category_request,
        )
    final = next(path for path in papers.iterdir() if path.is_dir() and not path.name.startswith("."))
    if damage == "missing":
        import shutil
        shutil.rmtree(final)
    else:
        next(final.glob("*.catalog.json")).unlink()

    with pytest.raises(CommitRecoveryCorruptionError):
        reconcile_commits(
            transactions_dir=tmp_path / "transactions",
            paper_raw_root=tmp_path / "paper_raw",
            papers_dir=papers,
            ledger_path=ledger_path,
            catalog_root=catalog_root,
            apply=apply,
        )
    assert sentinel.read_text(encoding="utf-8") == "keep"
    journal = next((tmp_path / "transactions" / "commit").glob("*.json"))
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "category_reconcile_requested"
