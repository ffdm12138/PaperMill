"""Unit tests for FormalPaperRegistry error handling.

Tests all known error conditions in FormalPaperRegistry.load().
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.catalog_folders.formal_registry import FormalPaper, FormalPaperRegistry
from src.library.paper_number_ledger import PaperNumberLedger


# ── helpers ──────────────────────────────────────────────────────────

def _write_ledger(ledger_path: Path, items: dict) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps({"items": items}), encoding="utf-8")


def _make_paper_dir(papers_dir: Path, paper_number: str, paper_name: str,
                    with_marker: bool = True, with_catalog: bool = True,
                    marker_paper_number: str | None = None,
                    marker_folder_name: str | None = None,
                    catalog_paper_number: str | None = None,
                    catalog_paper_name: str | None = None) -> Path:
    folder = papers_dir / paper_name
    folder.mkdir(parents=True, exist_ok=True)

    if with_marker:
        marker = {
            "schema_version": "1.0",
            "paper_number": marker_paper_number or paper_number,
            "folder_name": marker_folder_name or paper_name,
            "state": "active",
        }
        (folder / f"{paper_number}.paper.number").write_text(
            json.dumps(marker, ensure_ascii=False), encoding="utf-8")

    if with_catalog:
        catalog = {
            "schema_version": "3.2",
            "paper_number": catalog_paper_number or paper_number,
            "paper_name": catalog_paper_name or paper_name,
            "content_identity": {"content_title_zh": "测试"},
            "abstract": {"one_sentence_zh": "测试"},
            "methods": {"overview_zh": "测试"},
            "key_findings": [],
            "writing_value": {"use_cases": []},
            "screening": {"read_decision": "pending"},
            "figures_and_tables": [],
        }
        (folder / f"{paper_name}.catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False), encoding="utf-8")

    return folder


# ── tests ────────────────────────────────────────────────────────────

def test_invalid_paper_number_length(tmp_path):
    """paper_number not exactly 16 digits raises error."""
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    ledger_path = tmp_path / "ledger.json"

    _write_ledger(ledger_path, {
        "00000000000000001": {  # 17 digits
            "paper_number": "00000000000000001",
            "paper_name": "test_paper",
            "folder_name": "test_paper",
            "state": "active",
        },
    })

    reg = FormalPaperRegistry(papers_dir=papers_dir, ledger=PaperNumberLedger(ledger_path))
    with pytest.raises(ValueError, match="invalid active paper_number"):
        reg.load(refresh=True)


def test_ledger_paper_name_mismatch_folder(tmp_path):
    """Ledger paper_name != folder_name raises error."""
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    ledger_path = tmp_path / "ledger.json"

    _write_ledger(ledger_path, {
        "0000000000000001": {
            "paper_number": "0000000000000001",
            "paper_name": "test_paper",
            "folder_name": "different_name",  # mismatch
            "state": "active",
        },
    })

    reg = FormalPaperRegistry(papers_dir=papers_dir, ledger=PaperNumberLedger(ledger_path))
    with pytest.raises(ValueError, match="identity mismatch"):
        reg.load(refresh=True)


def test_duplicate_paper_name(tmp_path):
    """Two active entries with same paper_name raises error."""
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    ledger_path = tmp_path / "ledger.json"

    _write_ledger(ledger_path, {
        "0000000000000001": {
            "paper_number": "0000000000000001",
            "paper_name": "same_name",
            "folder_name": "same_name",
            "state": "active",
        },
        "0000000000000002": {
            "paper_number": "0000000000000002",
            "paper_name": "same_name",  # duplicate
            "folder_name": "same_name",
            "state": "active",
        },
    })

    # Create the directory so it gets past the directory check
    _make_paper_dir(papers_dir, "0000000000000001", "same_name")

    reg = FormalPaperRegistry(papers_dir=papers_dir, ledger=PaperNumberLedger(ledger_path))
    with pytest.raises(ValueError, match="duplicate active paper_name"):
        reg.load(refresh=True)


def test_duplicate_paper_number(tmp_path):
    """Two ledger entries with same paper_number (would be dup key in JSON but test edge case)."""
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    ledger_path = tmp_path / "ledger.json"

    # JSON dict can't have duplicate keys, so this is valid by construction
    _write_ledger(ledger_path, {
        "0000000000000001": {
            "paper_number": "0000000000000001",
            "paper_name": "test_paper",
            "folder_name": "test_paper",
            "state": "active",
        },
    })

    _make_paper_dir(papers_dir, "0000000000000001", "test_paper")

    reg = FormalPaperRegistry(papers_dir=papers_dir, ledger=PaperNumberLedger(ledger_path))
    papers = reg.load(refresh=True)
    assert len(papers) == 1


def test_orphan_formal_folder(tmp_path):
    """Directory in papers/ not in ledger raises error."""
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    ledger_path = tmp_path / "ledger.json"

    # Empty ledger
    _write_ledger(ledger_path, {})

    # Create orphan directory
    (papers_dir / "orphan_paper").mkdir()

    reg = FormalPaperRegistry(papers_dir=papers_dir, ledger=PaperNumberLedger(ledger_path))
    with pytest.raises(ValueError, match="orphan formal directories"):
        reg.load(refresh=True)


def test_missing_catalog(tmp_path):
    """Formal folder without catalog file raises error."""
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    ledger_path = tmp_path / "ledger.json"

    _write_ledger(ledger_path, {
        "0000000000000001": {
            "paper_number": "0000000000000001",
            "paper_name": "test_paper",
            "folder_name": "test_paper",
            "state": "active",
        },
    })

    _make_paper_dir(papers_dir, "0000000000000001", "test_paper",
                    with_marker=True, with_catalog=False)

    reg = FormalPaperRegistry(papers_dir=papers_dir, ledger=PaperNumberLedger(ledger_path))
    with pytest.raises(ValueError, match="catalog missing"):
        reg.load(refresh=True)


def test_missing_marker(tmp_path):
    """Formal folder without .paper.number marker raises error."""
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    ledger_path = tmp_path / "ledger.json"

    _write_ledger(ledger_path, {
        "0000000000000001": {
            "paper_number": "0000000000000001",
            "paper_name": "test_paper",
            "folder_name": "test_paper",
            "state": "active",
        },
    })

    _make_paper_dir(papers_dir, "0000000000000001", "test_paper",
                    with_marker=False, with_catalog=True)

    reg = FormalPaperRegistry(papers_dir=papers_dir, ledger=PaperNumberLedger(ledger_path))
    with pytest.raises(ValueError, match="exactly one marker"):
        reg.load(refresh=True)


def test_marker_paper_name_mismatch(tmp_path):
    """Marker folder_name != actual folder name raises error."""
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    ledger_path = tmp_path / "ledger.json"

    _write_ledger(ledger_path, {
        "0000000000000001": {
            "paper_number": "0000000000000001",
            "paper_name": "test_paper",
            "folder_name": "test_paper",
            "state": "active",
        },
    })

    _make_paper_dir(papers_dir, "0000000000000001", "test_paper",
                    marker_folder_name="wrong_name")

    reg = FormalPaperRegistry(papers_dir=papers_dir, ledger=PaperNumberLedger(ledger_path))
    with pytest.raises(ValueError, match="marker folder_name mismatch"):
        reg.load(refresh=True)


def test_catalog_paper_name_mismatch(tmp_path):
    """Catalog paper_name != folder name raises error."""
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    ledger_path = tmp_path / "ledger.json"

    _write_ledger(ledger_path, {
        "0000000000000001": {
            "paper_number": "0000000000000001",
            "paper_name": "test_paper",
            "folder_name": "test_paper",
            "state": "active",
        },
    })

    _make_paper_dir(papers_dir, "0000000000000001", "test_paper",
                    catalog_paper_name="wrong_name")

    reg = FormalPaperRegistry(papers_dir=papers_dir, ledger=PaperNumberLedger(ledger_path))
    with pytest.raises(ValueError, match="catalog paper_name mismatch"):
        reg.load(refresh=True)


def test_catalog_paper_number_mismatch(tmp_path):
    """Catalog paper_number != ledger paper_number raises error."""
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    ledger_path = tmp_path / "ledger.json"

    _write_ledger(ledger_path, {
        "0000000000000001": {
            "paper_number": "0000000000000001",
            "paper_name": "test_paper",
            "folder_name": "test_paper",
            "state": "active",
        },
    })

    _make_paper_dir(papers_dir, "0000000000000001", "test_paper",
                    catalog_paper_number="0000000000000099")

    reg = FormalPaperRegistry(papers_dir=papers_dir, ledger=PaperNumberLedger(ledger_path))
    with pytest.raises(ValueError, match="catalog paper_number mismatch"):
        reg.load(refresh=True)


def test_valid_registry_load(tmp_path):
    """Clean setup loads successfully."""
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    ledger_path = tmp_path / "ledger.json"

    _write_ledger(ledger_path, {
        "0000000000000001": {
            "paper_number": "0000000000000001",
            "paper_name": "test_paper",
            "folder_name": "test_paper",
            "state": "active",
        },
    })

    _make_paper_dir(papers_dir, "0000000000000001", "test_paper")

    reg = FormalPaperRegistry(papers_dir=papers_dir, ledger=PaperNumberLedger(ledger_path))
    papers = reg.load(refresh=True)
    assert len(papers) == 1
    assert papers[0].paper_number == "0000000000000001"
    assert papers[0].paper_name == "test_paper"


def test_resolve_by_paper_number(tmp_path):
    """resolve() finds paper by paper_number."""
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    ledger_path = tmp_path / "ledger.json"

    _write_ledger(ledger_path, {
        "0000000000000001": {
            "paper_number": "0000000000000001",
            "paper_name": "test_paper",
            "folder_name": "test_paper",
            "state": "active",
        },
    })

    _make_paper_dir(papers_dir, "0000000000000001", "test_paper")

    reg = FormalPaperRegistry(papers_dir=papers_dir, ledger=PaperNumberLedger(ledger_path))
    paper = reg.resolve("0000000000000001")
    assert paper is not None
    assert paper.paper_name == "test_paper"


def test_resolve_by_paper_name(tmp_path):
    """resolve() finds paper by paper_name."""
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    ledger_path = tmp_path / "ledger.json"

    _write_ledger(ledger_path, {
        "0000000000000001": {
            "paper_number": "0000000000000001",
            "paper_name": "test_paper",
            "folder_name": "test_paper",
            "state": "active",
        },
    })

    _make_paper_dir(papers_dir, "0000000000000001", "test_paper")

    reg = FormalPaperRegistry(papers_dir=papers_dir, ledger=PaperNumberLedger(ledger_path))
    paper = reg.resolve("test_paper")
    assert paper is not None
    assert paper.paper_number == "0000000000000001"
