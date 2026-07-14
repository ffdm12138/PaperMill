"""End-to-end catalog folder classification lifecycle tests.

Covers: keyword notebook → sync registry → create category folders →
formal papers enter all → enter pending → plan tasks → fake LLM classify →
apply results → assignments saved → links created → pending cleared →
writer reads categories.

Uses mocked FormalPaperRegistry to avoid the full formal-paper validation
requirements (freeze receipts, asset manifests, etc.).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.catalog_folders.assignment import load_assignment, valid_decisions
from src.catalog_folders.classifier_runner import run_tasks
from src.catalog_folders.testing.fake_classifier import FakeClassifier
from src.catalog_folders.formal_registry import FormalPaper, FormalPaperRegistry
from src.catalog_folders.models import CLASSIFIER_SKILL_VERSION, Category
from src.catalog_folders.reader import CatalogFolderReader
from src.catalog_folders.reconcile import reconcile_catalog_folders, reconcile_paper_membership
from src.discovery.keyword_notebook import KeywordNotebookStore, keyword_id as derive_keyword_id
from src.catalog_folders.registry import (
    category_from_notebook,
    definition_hash,
    load_categories,
    sync_registry,
)
from src.catalog_folders.result_validator import apply_result
from src.catalog_folders.task_planner import plan_tasks
from src.catalog_folders.validation import doctor
from src.library.paper_number_ledger import PaperNumberLedger


# ── helpers ──────────────────────────────────────────────────────────

def _write_notebook(notebook_dir: Path, keyword: str, is_chinese: bool = True, *, keyword_id: str | None = None) -> Path:
    """Write a complete bilingual schema-v3 notebook."""
    if keyword_id is None:
        keyword_id = derive_keyword_id(keyword)
    if not is_chinese or keyword_id != derive_keyword_id(keyword):
        raise ValueError("fixture requires canonical Chinese identity")
    store = KeywordNotebookStore(notebook_dir)
    store.create_notebook(keyword, search_queries=[
        {"query": keyword, "language": "zh", "source": "pytest"},
        {"query": f"english topic {keyword_id}", "language": "en", "source": "pytest"},
    ])
    return store._path_for(keyword)


def _make_formal_paper(papers_dir: Path, paper_number: str, paper_name: str) -> FormalPaper:
    """Create a minimal formal paper directory with catalog for testing."""
    folder = papers_dir / paper_name
    folder.mkdir(parents=True)
    catalog_path = folder / f"{paper_name}.catalog.json"
    cat = {
        "schema_version": "3.2", "paper_number": paper_number, "paper_name": paper_name,
        "content_identity": {
            "content_title_zh": f"测试论文{paper_number}",
            "research_domains": ["风雪物理"],
        },
        "abstract": {"one_sentence_zh": "一篇关于风吹雪的测试论文"},
        "methods": {"overview_zh": "数值模拟方法"},
        "key_findings": [{"finding_zh": "发现了重要规律"}],
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
    """Create a FormalPaperRegistry whose load() returns the given papers."""
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
    """Create a complete isolated catalog folder environment."""
    root = tmp_path / "catalog"
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir(parents=True)
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir(parents=True)

    # State directories
    (root / ".state").mkdir(parents=True)
    (root / "all").mkdir(parents=True)
    (root / "_pending").mkdir(parents=True)
    (root / ".state" / "tasks").mkdir(parents=True)
    (root / ".state" / "assignments").mkdir(parents=True)
    (root / ".state" / "applied_results").mkdir(parents=True)
    (root / ".state" / "locks").mkdir(parents=True)
    (root / ".state" / "results").mkdir(parents=True)
    (root / ".state" / "assignment_history").mkdir(parents=True)

    return {
        "root": root, "papers_dir": papers_dir, "notebook_dir": notebook_dir,
    }


# ── lifecycle tests ──────────────────────────────────────────────────

def test_full_classification_lifecycle(catalog_env):
    """Keyword notebook → sync → folders → formal papers → plan → classify → apply → pending cleared."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    # 1. Create keyword notebooks
    _write_notebook(notebook_dir, "风吹雪")
    _write_notebook(notebook_dir, "雪粒破碎")

    # 2. Sync registry
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)
    cats = load_categories(root / ".state" / "category_registry.json")
    assert len(cats) == 2

    # 3. Create formal papers
    paper_a = _make_formal_paper(papers_dir, "0000000000000001", "2024_author_test_paper_1")
    paper_b = _make_formal_paper(papers_dir, "0000000000000002", "2024_author_test_paper_2")
    paper_c = _make_formal_paper(papers_dir, "0000000000000003", "2024_author_test_paper_3")
    all_papers = [paper_a, paper_b, paper_c]
    reg = _mock_registry(all_papers, papers_dir)

    # 4. Reconcile → all/ created
    report = reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)
    assert report["formal_papers"] == 3
    assert set(report["all"]["added"]) == {"2024_author_test_paper_1", "2024_author_test_paper_2", "2024_author_test_paper_3"}
    assert len(report["pending"]["added"]) == 3  # all pending (no decisions yet)

    # 5. Plan tasks
    tasks = plan_tasks(root=root, formal_registry=reg, apply=True)
    assert len(tasks) == 3  # one task per paper

    # 6. Run fake classifier
    classifier = FakeClassifier()
    result = run_tasks(root=root, formal_registry=reg, classifier=classifier, apply=True)
    assert result["classified"] == 3
    assert result["errors"] == 0

    # 7. Verify assignments saved
    for paper in all_papers:
        assignment = load_assignment(root / ".state" / "assignments" / f"{paper.paper_number}.json")
        assert assignment is not None
        decisions = valid_decisions(assignment, paper, cats)
        assert len(decisions) == len(cats)
        for d in decisions.values():
            assert d["matched"] is True

    # 8. run_tasks already reconciled; verify category links exist on disk
    for cat in cats:
        cat_dir = root / cat.directory_name
        members = [p.name for p in cat_dir.iterdir() if p.name != ".category.json"]
        assert len(members) == 3  # all papers matched by fake classifier
    # Reconcile again should be a no-op (all already in place)
    report2 = reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)
    assert report2["pending_count"] == 0

    # 9. Doctor reports clean
    d = doctor(root=root, formal_registry=reg, notebook_dir=notebook_dir)
    assert d["writer_category_safe"] is True
    assert d["pending"] == 0
    assert d["classification_complete"] is True
    assert d["folder_integrity_safe"] is True

    # 10. Writer can read categories
    reader = CatalogFolderReader(root=root, papers_dir=papers_dir)
    papers_all = reader.list_papers(["all"])
    assert len(papers_all) == 3
    papers_cat1 = reader.list_papers(["风吹雪"])
    assert len(papers_cat1) == 3


def test_paper_with_no_matches_only_in_all(catalog_env):
    """Paper with matched=False → only in all, not in any keyword folder."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)
    cats = load_categories(root / ".state" / "category_registry.json")
    assert len(cats) == 1

    paper_a = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper_a")
    paper_b = _make_formal_paper(papers_dir, "0000000000000002", "2024_test_paper_b")
    reg = _mock_registry([paper_a, paper_b], papers_dir)

    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)
    plan_tasks(root=root, formal_registry=reg, apply=True)

    # Only apply result for paper_a (matched=False)
    task_files = list((root / ".state" / "tasks" / "0000000000000001").glob("*.json"))
    assert len(task_files) == 1
    task = json.loads(task_files[0].read_text(encoding="utf-8"))
    result_data = {
        "schema_version": "1.0",
        "task_id": task["task_id"],
        "task_input_sha256": task["task_input_sha256"],
        "paper_number": "0000000000000001",
        "paper_name": "2024_test_paper_a",
        "decisions": [{
            "category_id": cats[0].category_id,
            "matched": False,
            "confidence": "high",
            "reason_zh": "不相关",
            "catalog_evidence_fields": ["content_identity"],
        }],
    }
    result_path = root / ".state" / "results" / "0000000000000001" / f"{task['task_id']}.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result_data, ensure_ascii=False), encoding="utf-8")
    apply_result(result_path=result_path, root=root, formal_registry=reg, apply=True)

    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)

    # paper_a has matched=False → not in keyword folder
    cat_dir = root / cats[0].directory_name
    assert cat_dir.is_dir()
    members = [p.name for p in cat_dir.iterdir() if p.name != ".category.json"]
    assert "2024_test_paper_a" not in members  # matched=False
    # paper_b is still pending
    pending_members = [p.name for p in (root / "_pending").iterdir() if p.name != ".category.json"]
    assert "2024_test_paper_b" in pending_members

    # Both are in all
    all_members = [p.name for p in (root / "all").iterdir() if p.name != ".category.json"]
    assert "2024_test_paper_a" in all_members
    assert "2024_test_paper_b" in all_members


def test_empty_categories_with_papers_fail_closed(catalog_env):
    """Formal papers exist but 0 categories → fail closed."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]

    paper = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper")
    reg = _mock_registry([paper], papers_dir)

    # reconcile without categories should fail
    with pytest.raises(ValueError, match="0 active categories"):
        reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)

    # with allow_empty_categories, it should succeed
    report = reconcile_catalog_folders(root=root, formal_registry=reg, apply=True, allow_empty_categories=True)
    assert report["formal_papers"] == 1


def test_pending_writer_fail_closed(catalog_env):
    """Writer refuses category-filtered read when papers are pending; 'all' always works."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)
    paper = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper")
    reg = _mock_registry([paper], papers_dir)
    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)

    reader = CatalogFolderReader(root=root, papers_dir=papers_dir)
    # "all" always works even when papers are pending
    papers = reader.list_papers(["all"])
    assert len(papers) == 1

    # Category-filtered read should fail when pending (requires writer_safe)
    reader_with_reg = CatalogFolderReader(
        root=root, papers_dir=papers_dir, formal_registry=reg,
    )
    with pytest.raises(RuntimeError, match="not writer-safe"):
        reader_with_reg.list_papers(["风吹雪"])

    # With allow_pending, should succeed
    papers2 = reader.list_papers(["all"], allow_pending=True)
    assert len(papers2) == 1


def test_new_category_only_generates_new_tasks(catalog_env):
    """Adding a new category only creates tasks for that category, not all."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    # First category + paper + classify
    _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)
    paper = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper_a")
    reg = _mock_registry([paper], papers_dir)
    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)
    tasks1 = plan_tasks(root=root, formal_registry=reg, apply=True)
    assert len(tasks1) == 1

    # Classify with fake
    run_tasks(root=root, formal_registry=reg, classifier=FakeClassifier(), apply=True)
    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)

    # Now add a second category
    _write_notebook(notebook_dir, "雪粒破碎")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)

    # Plan tasks — should only have tasks for the NEW category
    tasks2 = plan_tasks(root=root, formal_registry=reg, apply=True)
    assert len(tasks2) == 1
    task_cat_ids = {c["category_id"] for c in tasks2[0]["categories"]}
    assert "ace250fe675fc00d" in task_cat_ids
    assert "2211dcaa01587d44" not in task_cat_ids  # already classified


def test_catalog_change_reclassify(catalog_env):
    """Changing a paper's Catalog invalidates old decisions."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)
    cats = load_categories(root / ".state" / "category_registry.json")

    paper = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper")
    reg = _mock_registry([paper], papers_dir)
    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)
    plan_tasks(root=root, formal_registry=reg, apply=True)
    run_tasks(root=root, formal_registry=reg, classifier=FakeClassifier(), apply=True)
    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)

    # Verify decisions are valid
    assignment = load_assignment(root / ".state" / "assignments" / f"{paper.paper_number}.json")
    decisions = valid_decisions(assignment, paper, cats)
    assert len(decisions) == 1

    # Change catalog
    catalog_path = paper.catalog_path
    cat_data = json.loads(catalog_path.read_text(encoding="utf-8"))
    cat_data["content_identity"]["content_title_zh"] = "修改后的标题"
    catalog_path.write_text(json.dumps(cat_data, ensure_ascii=False), encoding="utf-8")

    # Old decisions are now stale
    decisions2 = valid_decisions(assignment, paper, cats)
    assert len(decisions2) == 0

    # Plan new tasks
    tasks = plan_tasks(root=root, formal_registry=reg, apply=True)
    assert len(tasks) == 1

    # Apply with force (catalog changed → old decisions invalidated, new task planned)
    fake = FakeClassifier()
    fake_result = fake.classify(task=tasks[0], catalog=cat_data)
    result_path = root / ".state" / "results" / paper.paper_number / f"{tasks[0]['task_id']}.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(fake_result, ensure_ascii=False), encoding="utf-8")
    applied = apply_result(result_path=result_path, root=root, formal_registry=reg, apply=True, force=True)
    assert applied["status"] in ("applied", "applied_with_replacements")
    # Verify new decisions are valid
    new_assignment = load_assignment(root / ".state" / "assignments" / f"{paper.paper_number}.json")
    new_decisions = valid_decisions(new_assignment, paper, cats)
    assert len(new_decisions) == 1


def test_result_idempotency(catalog_env):
    """Same result applied twice returns already_applied."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)
    paper = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper")
    reg = _mock_registry([paper], papers_dir)
    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)
    plan_tasks(root=root, formal_registry=reg, apply=True)

    # Run fake classifier (writes result and applies)
    run_tasks(root=root, formal_registry=reg, classifier=FakeClassifier(), apply=True)

    # Run again — should skip (already applied)
    result2 = run_tasks(root=root, formal_registry=reg, classifier=FakeClassifier(), apply=True)
    assert result2["classified"] == 0
    assert result2["skipped"] == 1


def test_doctor_split_safety_flags(catalog_env):
    """Doctor reports proper split safety flags."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    # Empty state
    empty_reg = _mock_registry([], papers_dir)
    d = doctor(root=root, formal_registry=empty_reg)
    assert d["active_formal_papers"] == 0
    assert d["writer_category_safe"] is True  # no papers = safe

    # Papers without categories
    paper = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper")
    reg = _mock_registry([paper], papers_dir)
    d2 = doctor(root=root, formal_registry=reg)
    assert d2["writer_category_safe"] is False
    assert d2["folder_integrity_safe"] is False  # all mismatch

    # Add categories and reconcile
    _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)
    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)
    d3 = doctor(root=root, formal_registry=reg)
    assert d3["classification_complete"] is False  # pending
    assert d3["writer_category_safe"] is False

    # Classify
    plan_tasks(root=root, formal_registry=reg, apply=True)
    run_tasks(root=root, formal_registry=reg, classifier=FakeClassifier(), apply=True)
    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)
    d4 = doctor(root=root, formal_registry=reg)
    assert d4["classification_complete"] is True
    assert d4["writer_category_safe"] is True
    assert d4["folder_integrity_safe"] is True


def test_concurrent_results_dont_lose_decisions(catalog_env):
    """Two task batches for the same paper don't lose each other's decisions."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    # Create 2 categories
    _write_notebook(notebook_dir, "风吹雪")
    _write_notebook(notebook_dir, "雪粒破碎")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)
    cats = load_categories(root / ".state" / "category_registry.json")

    paper = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper")
    reg = _mock_registry([paper], papers_dir)
    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)

    # Plan tasks with max 1 category per task → 2 tasks
    tasks = plan_tasks(root=root, formal_registry=reg, apply=True, max_categories_per_task=1)
    assert len(tasks) == 2

    # Apply them sequentially (simulating concurrent via lock)
    fake = FakeClassifier()
    for task in tasks:
        cat_data = json.loads(paper.catalog_path.read_text(encoding="utf-8"))
        fake_result = fake.classify(task=task, catalog=cat_data)
        result_path = root / ".state" / "results" / paper.paper_number / f"{task['task_id']}.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(fake_result, ensure_ascii=False), encoding="utf-8")
        apply_result(result_path=result_path, root=root, formal_registry=reg, apply=True)

    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)

    # Verify both decisions are present
    assignment = load_assignment(root / ".state" / "assignments" / f"{paper.paper_number}.json")
    decisions = valid_decisions(assignment, paper, cats)
    assert len(decisions) == 2


def test_export_import_batch(catalog_env):
    """Export batch → import results workflow."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)
    paper = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper")
    reg = _mock_registry([paper], papers_dir)
    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)
    plan_tasks(root=root, formal_registry=reg, apply=True)

    from src.catalog_folders.classifier_runner import export_batch, import_results

    # Export
    export_path = root / ".state" / "export" / "classification_batch.json"
    batch = export_batch(root=root, output_path=export_path)
    assert batch["exported"] == 1

    # Check batch content
    batch_data = json.loads(export_path.read_text(encoding="utf-8"))
    assert batch_data["task_count"] == 1
    assert len(batch_data["tasks"]) == 1

    # Create fake results in a result directory
    result_dir = root.parent / "import_results"
    task = batch_data["tasks"][0]
    result_paper_dir = result_dir / task["paper_number"]
    result_paper_dir.mkdir(parents=True)

    fake = FakeClassifier()
    cat_data = json.loads(paper.catalog_path.read_text(encoding="utf-8"))
    fake_result = fake.classify(task=task, catalog=cat_data)
    (result_paper_dir / f"{task['task_id']}.json").write_text(
        json.dumps(fake_result, ensure_ascii=False), encoding="utf-8")

    # Import
    report = import_results(result_dir=result_dir, root=root, formal_registry=reg, apply=True)
    assert report["classified"] == 1
    assert report["errors"] == 0


def test_reconcile_paper_membership_single_paper(catalog_env):
    """reconcile_paper_membership handles a single paper's links atomically."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)
    cats = load_categories(root / ".state" / "category_registry.json")

    paper = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper")

    # Create assignment with matched=True
    assignment = {
        "schema_version": "1.0",
        "paper_number": paper.paper_number,
        "paper_name": paper.paper_name,
        "catalog_sha256": __import__("src.file_fingerprint", fromlist=["compute_sha256"]).compute_sha256(paper.catalog_path),
        "decisions": {
            cats[0].category_id: {
                "category_definition_sha256": cats[0].definition_sha256,
                "matched": True,
                "classifier_skill_version": CLASSIFIER_SKILL_VERSION,
            },
        },
    }
    (root / ".state" / "assignments").mkdir(parents=True, exist_ok=True)
    (root / ".state" / "assignments" / f"{paper.paper_number}.json").write_text(
        json.dumps(assignment, ensure_ascii=False), encoding="utf-8")

    report = reconcile_paper_membership(
        paper=paper, assignment=assignment, categories=cats, root=root, apply=True,
    )
    assert "all" in report["added"]
    assert cats[0].category_id in report["added"]


def test_rollback_removes_paper_from_all_categories(catalog_env):
    """After rollback, paper is removed from all, pending, and all category folders."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    _write_notebook(notebook_dir, "风吹雪")
    _write_notebook(notebook_dir, "雪粒破碎")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)
    cats = load_categories(root / ".state" / "category_registry.json")

    paper = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper")
    reg = _mock_registry([paper], papers_dir)
    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)
    plan_tasks(root=root, formal_registry=reg, apply=True)
    run_tasks(root=root, formal_registry=reg, classifier=FakeClassifier(), apply=True)
    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)

    # Verify paper is in all and categories
    all_dir = root / "all"
    assert (all_dir / "2024_test_paper").exists()
    for cat in cats:
        assert (root / cat.directory_name / "2024_test_paper").exists()

    # Simulate rollback: reconcile with empty registry removes the paper
    empty_reg = _mock_registry([], papers_dir)
    reconcile_catalog_folders(root=root, formal_registry=empty_reg, apply=True)

    # Paper should be removed from all and categories
    assert not (all_dir / "2024_test_paper").exists()
    for cat in cats:
        assert not (root / cat.directory_name / "2024_test_paper").exists()
    assert not (root / "_pending" / "2024_test_paper").exists()


def test_single_member_reconcile_does_not_delete_other_papers(catalog_env):
    """Category has A, B, C; updating B must not delete A and C."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)
    cats = load_categories(root / ".state" / "category_registry.json")

    paper_a = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper_a")
    paper_b = _make_formal_paper(papers_dir, "0000000000000002", "2024_test_paper_b")
    paper_c = _make_formal_paper(papers_dir, "0000000000000003", "2024_test_paper_c")
    reg = _mock_registry([paper_a, paper_b, paper_c], papers_dir)

    # Full reconcile: all papers get all/ and category links
    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)
    plan_tasks(root=root, formal_registry=reg, apply=True)
    run_tasks(root=root, formal_registry=reg, classifier=FakeClassifier(), apply=True)

    # All 3 in category
    cat_dir = root / cats[0].directory_name
    members = sorted(p.name for p in cat_dir.iterdir() if p.name != ".category.json")
    assert members == ["2024_test_paper_a", "2024_test_paper_b", "2024_test_paper_c"]

    # Now update only paper B using reconcile_paper_membership
    b_assignment = load_assignment(root / ".state" / "assignments" / "0000000000000002.json")
    from src.catalog_folders.reconcile import reconcile_paper_membership
    reconcile_paper_membership(
        paper=paper_b, assignment=b_assignment, categories=cats, root=root, apply=True,
    )

    # A and C must still be present
    members_after = sorted(p.name for p in cat_dir.iterdir() if p.name != ".category.json")
    assert members_after == ["2024_test_paper_a", "2024_test_paper_b", "2024_test_paper_c"]


def test_single_member_remove_only_removes_that_paper(catalog_env):
    """B changes from matched=true to false; only B removed, A and C kept."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)
    cats = load_categories(root / ".state" / "category_registry.json")

    paper_a = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper_a")
    paper_b = _make_formal_paper(papers_dir, "0000000000000002", "2024_test_paper_b")
    paper_c = _make_formal_paper(papers_dir, "0000000000000003", "2024_test_paper_c")
    reg = _mock_registry([paper_a, paper_b, paper_c], papers_dir)

    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)
    plan_tasks(root=root, formal_registry=reg, apply=True)
    run_tasks(root=root, formal_registry=reg, classifier=FakeClassifier(), apply=True)

    cat_dir = root / cats[0].directory_name
    members_before = sorted(p.name for p in cat_dir.iterdir() if p.name != ".category.json")
    assert "2024_test_paper_b" in members_before

    # Change B's assignment to matched=False
    b_assignment = load_assignment(root / ".state" / "assignments" / "0000000000000002.json")
    b_assignment["decisions"][cats[0].category_id]["matched"] = False
    (root / ".state" / "assignments" / "0000000000000002.json").write_text(
        json.dumps(b_assignment, ensure_ascii=False), encoding="utf-8")

    # Reconcile only paper B
    from src.catalog_folders.reconcile import reconcile_paper_membership
    reconcile_paper_membership(
        paper=paper_b, assignment=b_assignment, categories=cats, root=root, apply=True,
    )

    # Only B removed; A and C still present
    members_after = sorted(p.name for p in cat_dir.iterdir() if p.name != ".category.json")
    assert "2024_test_paper_b" not in members_after
    assert "2024_test_paper_a" in members_after
    assert "2024_test_paper_c" in members_after


def test_many_categories_batched_no_lost_decisions(catalog_env):
    """45 categories batched 20/20/5; all 45 decisions survive regardless of order."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    # Create 45 categories — keyword_id is derived from keyword
    for i in range(45):
        _write_notebook(notebook_dir, f"测试分类{i}")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)
    cats = load_categories(root / ".state" / "category_registry.json")
    assert len(cats) == 45

    paper = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper")
    reg = _mock_registry([paper], papers_dir)
    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)

    # Plan with batching: max 20 per task → 3 tasks
    tasks = plan_tasks(root=root, formal_registry=reg, apply=True, max_categories_per_task=20)
    assert len(tasks) == 3

    # Apply all 3 tasks
    fake = FakeClassifier()
    for task in tasks:
        cat_data = json.loads(paper.catalog_path.read_text(encoding="utf-8"))
        fake_result = fake.classify(task=task, catalog=cat_data)
        result_path = root / ".state" / "results" / paper.paper_number / f"{task['task_id']}.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(fake_result, ensure_ascii=False), encoding="utf-8")
        apply_result(result_path=result_path, root=root, formal_registry=reg, apply=True)

    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)

    # All 45 decisions present
    assignment = load_assignment(root / ".state" / "assignments" / f"{paper.paper_number}.json")
    decisions = valid_decisions(assignment, paper, cats)
    assert len(decisions) == 45
    assert (root / "_pending" / "2024_test_paper").exists() is False
