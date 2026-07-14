"""Integration tests for validate_v2_library.

Tests the validate_v2_library() function with clean and broken library states.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.catalog_folders.formal_registry import FormalPaper, FormalPaperRegistry
from src.catalog_folders.link_backend import create_paper_link
from src.library.paper_number_ledger import PaperNumberLedger


# ── helpers ──────────────────────────────────────────────────────────

def _write_ledger(ledger_path: Path, items: dict) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps({"items": items}), encoding="utf-8")


def _make_paper(papers_dir: Path, paper_number: str, paper_name: str) -> FormalPaper:
    folder = papers_dir / paper_name
    folder.mkdir(parents=True, exist_ok=True)

    marker = {"schema_version": "1.0", "paper_number": paper_number,
               "folder_name": paper_name, "state": "active"}
    (folder / f"{paper_number}.paper.number").write_text(
        json.dumps(marker), encoding="utf-8")

    catalog = {
        "schema_version": "3.2", "paper_number": paper_number, "paper_name": paper_name,
        "content_identity": {"content_title_zh": "测试论文"},
        "abstract": {"one_sentence_zh": "一篇测试论文"},
        "methods": {"overview_zh": "数值方法"},
        "key_findings": [{"finding_zh": "发现了规律"}],
        "writing_value": {"use_cases": ["综述引用"]},
        "screening": {"read_decision": "pending"},
        "figures_and_tables": [],
    }
    (folder / f"{paper_name}.catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False), encoding="utf-8")

    return FormalPaper(
        paper_number=paper_number, paper_name=paper_name, directory=folder,
        catalog_path=folder / f"{paper_name}.catalog.json",
        metadata_path=folder / f"{paper_name}.metadata.json",
    )


# ── tests ────────────────────────────────────────────────────────────

def test_valid_library_passes(tmp_path):
    """Clean library with one complete paper produces valid=True."""
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    catalog_root = tmp_path / "catalog"
    catalog_root.mkdir()
    (catalog_root / ".state").mkdir(parents=True)
    (catalog_root / "all").mkdir(parents=True)
    (catalog_root / "_pending").mkdir(parents=True)

    ledger_path = tmp_path / "ledger.json"

    paper = _make_paper(papers_dir, "0000000000000001", "2024_test_paper")
    # Create all/ link
    create_paper_link(catalog_root / "all" / "2024_test_paper", paper.directory)

    _write_ledger(ledger_path, {
        "0000000000000001": {
            "paper_number": "0000000000000001",
            "paper_name": "2024_test_paper",
            "folder_name": "2024_test_paper",
            "state": "active",
        },
    })

    from scripts.validate_v2_library import validate_v2_library

    report = validate_v2_library(
        papers_dir=papers_dir,
        ledger_path=ledger_path,
        catalog_root=catalog_root,
    )

    # Report structure
    assert "valid" in report
    assert "active_formal_papers" in report
    assert "writer_category_safe" in report
    assert "folder_integrity_safe" in report
    assert "classification_complete" in report
    assert "errors" in report

    # With all/ link for the paper, folder integrity may be ok
    # but classification won't be complete (no categories, no assignments)
    assert report["active_formal_papers"] == 1


def test_pending_papers_invalid(tmp_path):
    """Pending papers in _pending produce classification_complete=False."""
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    catalog_root = tmp_path / "catalog"
    catalog_root.mkdir()
    (catalog_root / ".state").mkdir(parents=True)
    (catalog_root / "all").mkdir(parents=True)
    (catalog_root / "_pending").mkdir(parents=True)

    ledger_path = tmp_path / "ledger.json"

    paper = _make_paper(papers_dir, "0000000000000001", "2024_test_paper")
    # Create both all/ and _pending/ links
    create_paper_link(catalog_root / "all" / "2024_test_paper", paper.directory)
    create_paper_link(catalog_root / "_pending" / "2024_test_paper", paper.directory)

    _write_ledger(ledger_path, {
        "0000000000000001": {
            "paper_number": "0000000000000001",
            "paper_name": "2024_test_paper",
            "folder_name": "2024_test_paper",
            "state": "active",
        },
    })

    from scripts.validate_v2_library import validate_v2_library

    report = validate_v2_library(
        papers_dir=papers_dir,
        ledger_path=ledger_path,
        catalog_root=catalog_root,
    )

    # Paper is pending → classification not complete → not valid
    assert report["pending"] >= 1
    assert report["classification_complete"] is False
    assert report["writer_category_safe"] is False
    assert report["valid"] is False


def test_uses_writer_category_safe_key(tmp_path):
    """Report includes the writer_category_safe key."""
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    catalog_root = tmp_path / "catalog"
    catalog_root.mkdir()
    (catalog_root / ".state").mkdir(parents=True)
    (catalog_root / "all").mkdir(parents=True)
    (catalog_root / "_pending").mkdir(parents=True)

    ledger_path = tmp_path / "ledger.json"

    _write_ledger(ledger_path, {})

    from scripts.validate_v2_library import validate_v2_library

    report = validate_v2_library(
        papers_dir=papers_dir,
        ledger_path=ledger_path,
        catalog_root=catalog_root,
    )

    # Must contain writer_category_safe
    assert "writer_category_safe" in report
    # Must contain valid
    assert "valid" in report


def test_folder_integrity_failure(tmp_path):
    """All membership mismatch produces folder_integrity_safe=False."""
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    catalog_root = tmp_path / "catalog"
    catalog_root.mkdir()
    (catalog_root / ".state").mkdir(parents=True)
    (catalog_root / "all").mkdir(parents=True)
    (catalog_root / "_pending").mkdir(parents=True)

    ledger_path = tmp_path / "ledger.json"

    # Paper in ledger but no all/ link — all membership mismatch
    _make_paper(papers_dir, "0000000000000001", "2024_test_paper")

    _write_ledger(ledger_path, {
        "0000000000000001": {
            "paper_number": "0000000000000001",
            "paper_name": "2024_test_paper",
            "folder_name": "2024_test_paper",
            "state": "active",
        },
    })

    from scripts.validate_v2_library import validate_v2_library

    report = validate_v2_library(
        papers_dir=papers_dir,
        ledger_path=ledger_path,
        catalog_root=catalog_root,
    )

    # Missing all membership causes errors
    assert report["folder_integrity_safe"] is False
    assert report["valid"] is False
    assert len(report["errors"]) > 0
