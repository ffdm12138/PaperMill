"""Integration tests for writer catalog safety gate.

Tests CatalogFolderReader.assert_writer_safe() and list_papers() behavior
with various unsafe states. Uses the catalog_env fixture pattern.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.catalog_folders.formal_registry import FormalPaper, FormalPaperRegistry
from src.catalog_folders.reader import CatalogFolderReader
from src.discovery.keyword_notebook import KeywordNotebookStore, keyword_id as derive_keyword_id
from src.catalog_folders.registry import sync_registry
from src.catalog_folders.reconcile import reconcile_catalog_folders


# ── helpers ──────────────────────────────────────────────────────────

def _write_notebook(notebook_dir: Path, keyword: str, *,
                    keyword_id: str | None = None,
                    enabled: bool = True) -> Path:
    if keyword_id is None:
        keyword_id = derive_keyword_id(keyword)
    if keyword_id != derive_keyword_id(keyword):
        raise ValueError("test helper accepts only canonical keyword identity")
    store = KeywordNotebookStore(notebook_dir)
    store.create_notebook(keyword, enabled=enabled, search_queries=[
        {"query": keyword, "language": "zh", "source": "pytest"},
        {"query": "blowing snow", "language": "en", "source": "pytest"},
    ])
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


# ── safety gate tests ───────────────────────────────────────────────

def test_pending_nonempty_refuses_writer(catalog_env):
    """Papers in _pending cause writer to refuse non-all category access."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)

    paper = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper")
    reg = _mock_registry([paper], papers_dir)
    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)

    # Paper is in _pending (no classification)
    reader = CatalogFolderReader(root=root, papers_dir=papers_dir,
                                  formal_registry=reg, notebook_dir=notebook_dir)
    with pytest.raises(RuntimeError, match="not writer-safe"):
        reader.list_papers(["风吹雪"])


def test_registry_drift_refuses_writer(catalog_env):
    """Notebook-registry mismatch causes writer to refuse."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)

    # Manually add a registry-only entry (creates drift)
    raw = json.loads((root / ".state" / "category_registry.json").read_text(encoding="utf-8"))
    raw["categories"].append({
        "category_id": "ace250fe675fc00d",
        "keyword_zh": "雪粒破碎",
        "directory_name": "雪粒破碎",
        "source_notebook": "雪粒破碎.json",
        "definition_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "classification_enabled": True,
    })
    (root / ".state" / "category_registry.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    # Create dirs for both to avoid missing-dir errors
    (root / "风吹雪").mkdir(exist_ok=True)
    (root / "雪粒破碎").mkdir(exist_ok=True)

    paper = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper")
    reg = _mock_registry([paper], papers_dir)

    reader = CatalogFolderReader(root=root, papers_dir=papers_dir,
                                  formal_registry=reg, notebook_dir=notebook_dir)
    with pytest.raises(RuntimeError, match="not writer-safe"):
        reader.assert_writer_safe()


def test_corrupt_notebook_refuses_writer(catalog_env):
    """Corrupt notebook JSON causes writer to refuse."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    # Write a corrupt notebook
    (notebook_dir / "corrupt.json").write_text("{invalid json", encoding="utf-8")

    # Write a valid notebook for context (sync_registry would block on corrupt
    # notebook, so we test doctor's handling of bad notebooks directly)
    _write_notebook(notebook_dir, "风吹雪")

    paper = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper")
    reg = _mock_registry([paper], papers_dir)

    reader = CatalogFolderReader(root=root, papers_dir=papers_dir,
                                  formal_registry=reg, notebook_dir=notebook_dir)
    with pytest.raises(RuntimeError, match="not writer-safe"):
        reader.assert_writer_safe()


def test_unfinished_journal_refuses_writer(catalog_env):
    """Unfinished apply journal causes writer to refuse."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)
    (root / "风吹雪").mkdir(exist_ok=True)

    # Create an unfinished apply journal
    apply_journal_dir = root / ".state" / "apply_journal"
    apply_journal_dir.mkdir(parents=True)
    (apply_journal_dir / "unfinished.json").write_text(json.dumps({
        "schema_version": "1.0",
        "state": "in_progress",
        "updated_at": "2026-01-01T00:00:00Z",
    }), encoding="utf-8")

    paper = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper")
    reg = _mock_registry([paper], papers_dir)

    reader = CatalogFolderReader(root=root, papers_dir=papers_dir,
                                  formal_registry=reg, notebook_dir=notebook_dir,
                                  transaction_root=root / ".state")
    status = reader.status()
    assert len(status["unfinished_transactions"]) >= 1
    assert not status["writer_category_safe"]


def test_fully_safe_allows_writer(catalog_env):
    """Clean state allows writer to read categories."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)

    (root / "风吹雪").mkdir(exist_ok=True)

    paper = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper")
    reg = _mock_registry([paper], papers_dir)
    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)

    from src.catalog_folders.assignment import load_assignment, valid_decisions
    from src.catalog_folders.models import CLASSIFIER_SKILL_VERSION
    from src.discovery.keyword_notebook import keyword_id as derive_keyword_id
    from src.catalog_folders.registry import load_categories
    from src.file_fingerprint import compute_sha256

    # Write a complete assignment so nothing is pending
    cats = load_categories(root / ".state" / "category_registry.json")
    assignment = {
        "schema_version": "1.0",
        "paper_number": "0000000000000001",
        "paper_name": "2024_test_paper",
        "catalog_sha256": compute_sha256(paper.catalog_path),
        "decisions": {
            c.category_id: {
                "category_definition_sha256": c.definition_sha256,
                "matched": True,
                "classifier_skill_version": CLASSIFIER_SKILL_VERSION,
            }
            for c in cats
        },
    }
    (root / ".state" / "assignments" / "0000000000000001.json").write_text(
        json.dumps(assignment, ensure_ascii=False), encoding="utf-8")

    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)

    reader = CatalogFolderReader(root=root, papers_dir=papers_dir,
                                  formal_registry=reg, notebook_dir=notebook_dir)
    # Should not raise
    reader.assert_writer_safe()
    papers = reader.list_papers(["风吹雪"])
    assert len(papers) == 1


def test_all_category_bypasses_safety_gate(catalog_env):
    """Reading ["all"] bypasses the writer safety gate."""
    root = catalog_env["root"]
    papers_dir = catalog_env["papers_dir"]
    notebook_dir = catalog_env["notebook_dir"]

    _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=root / ".state" / "category_registry.json", apply=True)

    paper = _make_formal_paper(papers_dir, "0000000000000001", "2024_test_paper")
    reg = _mock_registry([paper], papers_dir)
    reconcile_catalog_folders(root=root, formal_registry=reg, apply=True)

    # Paper is pending but reading "all" should still work (safety gate is bypassed)
    reader = CatalogFolderReader(root=root, papers_dir=papers_dir,
                                  formal_registry=reg, notebook_dir=notebook_dir)
    papers = reader.list_papers(["all"])
    assert len(papers) == 1
