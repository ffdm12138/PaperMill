from __future__ import annotations

from pathlib import Path

import pytest

from src.discovery.relevance_runtime import RelevanceRuntimePaths


def _plan(tmp_path: Path) -> dict[str, str]:
    paths = RelevanceRuntimePaths.resolve_default(
        notebook_root=tmp_path / "notebooks",
        journal_root=tmp_path / "pages",
        transaction_root=tmp_path / "transactions" / "relevance_profiles",
    )
    return {
        "transaction_id": "tx",
        "resolved_notebook_root": str(paths.notebook_root),
        "resolved_journal_root": str(paths.journal_root),
        "resolved_transaction_root": str(paths.transaction_root),
        "resolved_lock_path": str(paths.lock_path),
        "transaction_journal_path": str(paths.transaction_root / "tx.json"),
    }


def test_from_plan_rejects_retired_duplicate_runtime_fields(tmp_path: Path):
    plan = _plan(tmp_path)
    plan["transaction_root"] = plan["resolved_transaction_root"]
    with pytest.raises(ValueError, match="retired"):
        RelevanceRuntimePaths.from_plan(plan)


def test_from_plan_rejects_any_resolved_path_drift(tmp_path: Path):
    plan = _plan(tmp_path)
    plan["resolved_lock_path"] = str(tmp_path / "other.lock")
    with pytest.raises(ValueError, match="drift"):
        RelevanceRuntimePaths.from_plan(plan)


def test_from_plan_validates_journal_location(tmp_path: Path):
    plan = _plan(tmp_path)
    plan["transaction_journal_path"] = str(tmp_path / "outside.json")
    with pytest.raises(ValueError, match="transaction root"):
        RelevanceRuntimePaths.from_plan(plan)
