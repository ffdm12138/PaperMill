"""Integration tests for apply result recovery.

Tests apply_result idempotency, hash validation, crash recovery via
receipts, and the conflict detection logic in the result validator.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.catalog_folders.formal_registry import FormalPaper, FormalPaperRegistry
from src.catalog_folders.models import CLASSIFIER_SKILL_VERSION, Category
from src.discovery.contracts.notebook import keyword_id as derive_keyword_id
from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore
from src.catalog_folders.registry import load_categories, sync_registry
from src.catalog_folders.reconcile import reconcile_catalog_folders
from src.catalog_folders.result_validator import apply_result
from src.catalog_folders.task_planner import plan_tasks, canonical_hash
from src.file_fingerprint import compute_sha256
from tests.helpers.relevance_profiles import bind_test_relevance_profile


# ── helpers ──────────────────────────────────────────────────────────

def _write_notebook(notebook_dir: Path, keyword: str, *,
                    keyword_id: str | None = None,
                    enabled: bool = True) -> Path:
    if keyword_id is None:
        keyword_id = derive_keyword_id(keyword)
    if keyword_id != derive_keyword_id(keyword):
        raise ValueError("fixture requires canonical keyword identity")
    store = KeywordNotebookStore(notebook_dir)
    store.create_notebook(keyword, enabled=False, search_queries=[
        {"query": keyword, "language": "zh", "source": "pytest"},
        {"query": f"english topic {keyword_id}", "language": "en", "source": "pytest"},
    ])
    bind_test_relevance_profile(store, keyword)
    if enabled:
        store.set_enabled(keyword, True)
    return store._path_for(keyword)


def _make_formal_paper(papers_dir: Path, paper_number: str, paper_name: str) -> FormalPaper:
    folder = papers_dir / paper_name
    folder.mkdir(parents=True)
    catalog_path = folder / f"{paper_name}.catalog.json"
    cat = {
        "schema_version": "3.2", "paper_number": paper_number, "paper_name": paper_name,
        "content_identity": {"content_title_zh": "测试论文"},
        "abstract": {"one_sentence_zh": "一篇测试论文"},
        "methods": {"overview_zh": "数值方法"},
        "key_findings": [{"finding_zh": "发现了规律"}],
        "writing_value": {"use_cases": ["综述引用"]},
        "screening": {"read_decision": "pending"},
        "figures_and_tables": [],
    }
    catalog_path.write_text(json.dumps(cat, ensure_ascii=False), encoding="utf-8")
    return FormalPaper(
        paper_number=paper_number, paper_name=paper_name, directory=folder,
        catalog_path=catalog_path,
        metadata_path=folder / f"{paper_name}.metadata.json",
    )


def _mock_registry(papers: list[FormalPaper], papers_dir: Path) -> FormalPaperRegistry:
    reg = MagicMock(spec=FormalPaperRegistry)
    reg.papers_dir = papers_dir
    reg.load = MagicMock(return_value=tuple(papers))
    reg.resolve = MagicMock(side_effect=lambda identity: next(
        (p for p in papers if identity in {p.paper_number, p.paper_name}), None
    ))
    return reg


# ── fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def catalog_env(tmp_path: Path) -> dict:
    root = tmp_path / "catalog"
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir(parents=True)
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir(parents=True)

    (root / ".state").mkdir(parents=True)
    (root / "all").mkdir(parents=True)
    (root / "_pending").mkdir(parents=True)
    (root / ".state" / "tasks").mkdir(parents=True)
    (root / ".state" / "assignments").mkdir(parents=True)
    (root / ".state" / "applied_results").mkdir(parents=True)
    (root / ".state" / "locks").mkdir(parents=True)
    (root / ".state" / "results").mkdir(parents=True)
    (root / ".state" / "assignment_history").mkdir(parents=True)

    return {"root": root, "papers_dir": papers_dir, "notebook_dir": notebook_dir}


# ── apply idempotency tests ─────────────────────────────────────────

def test_apply_result_idempotent(catalog_env):
    """Same result applied twice returns already_applied."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)
    cats = load_categories(root / ".state" / "category_registry.json")

    paper = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper")
    reg = _mock_registry([paper], papers_dir)
    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)
    tasks = plan_tasks(root=root, formal_registry=reg, apply=True)
    assert len(tasks) == 1

    task = tasks[0]
    catalog = json.loads(paper.catalog_path.read_text(encoding="utf-8"))

    # Build a valid result
    result = {
        "schema_version": "1.0",
        "task_id": task["task_id"],
        "task_input_sha256": task["task_input_sha256"],
        "paper_number": "0000000000000001",
        "paper_name": "2024_test_paper",
        "decisions": [{
            "category_id": cats[0].category_id,
            "matched": True,
            "confidence": "high",
            "reason_zh": "相关",
            "catalog_evidence_fields": list(catalog.keys())[:3],
        }],
    }

    result_path = root / ".state" / "results" / "0000000000000001" / f"{task['task_id']}.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    # First apply
    applied1 = apply_result(result_path=result_path, root=root, formal_registry=reg, apply=True)
    assert applied1["status"] in ("applied", "applied_with_replacements")

    # Second apply — idempotent
    applied2 = apply_result(result_path=result_path, root=root, formal_registry=reg, apply=True)
    assert applied2["status"] == "already_applied"


def test_hash_conflict_fail_closed(catalog_env):
    """Task hash mismatch causes failure."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)
    cats = load_categories(root / ".state" / "category_registry.json")

    paper = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper")
    reg = _mock_registry([paper], papers_dir)
    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)
    tasks = plan_tasks(root=root, formal_registry=reg, apply=True)
    task = tasks[0]

    # Result with wrong task_input_sha256
    result = {
        "schema_version": "1.0",
        "task_id": task["task_id"],
        "task_input_sha256": "0" * 64,  # wrong hash
        "paper_number": "0000000000000001",
        "paper_name": "2024_test_paper",
        "decisions": [{
            "category_id": cats[0].category_id,
            "matched": True,
            "confidence": "high",
            "reason_zh": "相关",
            "catalog_evidence_fields": ["content_identity"],
        }],
    }

    result_path = root / ".state" / "results" / "0000000000000001" / f"{task['task_id']}.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="hash mismatch"):
        apply_result(result_path=result_path, root=root, formal_registry=reg, apply=True)


def test_conflicting_replay_fail_closed(catalog_env):
    """Applying a different result with the same task_id fails."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)
    cats = load_categories(root / ".state" / "category_registry.json")

    paper = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper")
    reg = _mock_registry([paper], papers_dir)
    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)
    tasks = plan_tasks(root=root, formal_registry=reg, apply=True)
    task = tasks[0]
    catalog = json.loads(paper.catalog_path.read_text(encoding="utf-8"))

    # First result
    result1 = {
        "schema_version": "1.0",
        "task_id": task["task_id"],
        "task_input_sha256": task["task_input_sha256"],
        "paper_number": "0000000000000001",
        "paper_name": "2024_test_paper",
        "decisions": [{
            "category_id": cats[0].category_id,
            "matched": True,
            "confidence": "high",
            "reason_zh": "相关-版本1",
            "catalog_evidence_fields": list(catalog.keys())[:3],
        }],
    }

    # Second result (different reason_zh, same task)
    result2 = {
        "schema_version": "1.0",
        "task_id": task["task_id"],
        "task_input_sha256": task["task_input_sha256"],
        "paper_number": "0000000000000001",
        "paper_name": "2024_test_paper",
        "decisions": [{
            "category_id": cats[0].category_id,
            "matched": True,
            "confidence": "high",
            "reason_zh": "相关-版本2",  # different
            "catalog_evidence_fields": list(catalog.keys())[:3],
        }],
    }

    result_path = root / ".state" / "results" / "0000000000000001" / f"{task['task_id']}.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result1, ensure_ascii=False), encoding="utf-8")

    # Apply first
    apply_result(result_path=result_path, root=root, formal_registry=reg, apply=True)

    # Replace with different result
    result_path.write_text(json.dumps(result2, ensure_ascii=False), encoding="utf-8")

    # Should fail: different result hash -> conflicting replay
    with pytest.raises(RuntimeError, match="conflicting replay"):
        apply_result(result_path=result_path, root=root, formal_registry=reg, apply=True)


def test_applied_receipt_written_after_success(catalog_env):
    """After successful apply, receipt is written to disk."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)
    cats = load_categories(root / ".state" / "category_registry.json")

    paper = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper")
    reg = _mock_registry([paper], papers_dir)
    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)
    tasks = plan_tasks(root=root, formal_registry=reg, apply=True)
    task = tasks[0]
    catalog = json.loads(paper.catalog_path.read_text(encoding="utf-8"))

    result = {
        "schema_version": "1.0",
        "task_id": task["task_id"],
        "task_input_sha256": task["task_input_sha256"],
        "paper_number": "0000000000000001",
        "paper_name": "2024_test_paper",
        "decisions": [{
            "category_id": cats[0].category_id,
            "matched": True,
            "confidence": "high",
            "reason_zh": "相关",
            "catalog_evidence_fields": list(catalog.keys())[:3],
        }],
    }

    result_path = root / ".state" / "results" / "0000000000000001" / f"{task['task_id']}.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    apply_result(result_path=result_path, root=root, formal_registry=reg, apply=True)

    # Receipt should exist
    receipt_path = root / ".state" / "applied_results" / "0000000000000001" / f"{task['task_id']}.json"
    assert receipt_path.is_file()

    # Assignment should exist
    assignment_path = root / ".state" / "assignments" / "0000000000000001.json"
    assert assignment_path.is_file()


def test_catalog_change_after_task_plan_fail_closed(catalog_env):
    """If catalog changes after task planning, apply fails with stale hash."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)
    cats = load_categories(root / ".state" / "category_registry.json")

    paper = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper")
    reg = _mock_registry([paper], papers_dir)
    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)
    tasks = plan_tasks(root=root, formal_registry=reg, apply=True)
    task = tasks[0]
    catalog = json.loads(paper.catalog_path.read_text(encoding="utf-8"))

    # Change catalog AFTER task planning (hash in task is now stale)
    catalog["content_identity"]["content_title_zh"] = "修改后的标题"
    paper.catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")

    result = {
        "schema_version": "1.0",
        "task_id": task["task_id"],
        "task_input_sha256": task["task_input_sha256"],
        "paper_number": "0000000000000001",
        "paper_name": "2024_test_paper",
        "decisions": [{
            "category_id": cats[0].category_id,
            "matched": True,
            "confidence": "high",
            "reason_zh": "相关",
            "catalog_evidence_fields": ["content_identity"],
        }],
    }

    result_path = root / ".state" / "results" / "0000000000000001" / f"{task['task_id']}.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    # Should fail because catalog changed (hash stale)
    with pytest.raises(ValueError, match="Catalog hash is stale"):
        apply_result(result_path=result_path, root=root, formal_registry=reg, apply=True)


def test_force_allowes_stale_catalog(catalog_env):
    """force=True allows applying a result even when catalog hash differs."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)
    cats = load_categories(root / ".state" / "category_registry.json")

    paper = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper")
    reg = _mock_registry([paper], papers_dir)
    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)
    tasks = plan_tasks(root=root, formal_registry=reg, apply=True)
    task = tasks[0]
    catalog = json.loads(paper.catalog_path.read_text(encoding="utf-8"))

    # Change catalog
    catalog["content_identity"]["content_title_zh"] = "修改后"
    paper.catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")

    result = {
        "schema_version": "1.0",
        "task_id": task["task_id"],
        "task_input_sha256": task["task_input_sha256"],
        "paper_number": "0000000000000001",
        "paper_name": "2024_test_paper",
        "decisions": [{
            "category_id": cats[0].category_id,
            "matched": True,
            "confidence": "high",
            "reason_zh": "相关",
            "catalog_evidence_fields": ["content_identity"],
        }],
    }

    result_path = root / ".state" / "results" / "0000000000000001" / f"{task['task_id']}.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    # With force=True, should succeed
    applied = apply_result(result_path=result_path, root=root, formal_registry=reg,
                           apply=True, force=True)
    assert applied["status"] in ("applied", "applied_with_replacements")
