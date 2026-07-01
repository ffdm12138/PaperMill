from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.services.v2_library import V2PaperCommitService, empty_catalog, empty_metadata
from scripts.commit_paper_raw_to_papers import _ready_dirs


def _raw_folder(tmp_path: Path, pid: str = "2024_wang_手动导入") -> Path:
    folder = tmp_path / "paper_raw" / pid
    folder.mkdir(parents=True)
    metadata = empty_metadata(pid)
    parts = pid.split("_")
    short_name = "_".join(parts[2:]) or "手动导入"
    metadata["title"]["original"] = "Manual Import Paper"
    metadata["title"]["short_zh"] = short_name
    metadata["year"] = 2024
    metadata["authors"] = [{"full_name": "wang A", "family": parts[1] if len(parts) > 1 else "wang", "given": "A", "orcid": "", "affiliation": ""}]
    metadata["container"]["journal"] = "Manual Journal"
    metadata["identifiers"]["doi"] = "10.1000/manual-import"
    metadata["metadata_match"] = {
        "status": "manual_confirmed",
        "source": "test",
        "confidence": 1.0,
        "matched_at": "2026-01-01T00:00:00",
        "warnings": [],
        "candidates": [],
    }
    catalog = empty_catalog()
    catalog["content_identity"]["content_title"] = "手动导入论文内容标题"
    catalog["classification"]["primary_domain"] = "snow"
    catalog["screening"]["read_decision"] = "must_read"
    catalog["screening"]["reason"] = "该文献与中文综述主题直接相关。"
    catalog["research_card"].update({
        "research_problem": "研究手动导入文献的入库边界。",
        "core_question": "如何阻止不完整 metadata 入库？",
        "hypothesis_or_objective": "验证手动确认后的正式入库门禁。",
        "study_object": "手动导入文献",
        "method_summary": "使用 mock 文献资产验证 metadata gate。",
        "data_or_experiment": "临时 PDF、Markdown 与 JSON 资产。",
        "main_findings": ["metadata 不完整时不能入库。"],
        "mechanisms": ["commit 前统一 readiness gate。"],
        "limitations": ["只覆盖结构性入库检查。"],
        "usefulness_for_user": "保障正式库资产质量。",
    })
    catalog["content_notes"]["short_summary"] = "用于验证 catalog 草稿不能绕过 metadata gate。"
    (folder / f"{pid}.metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    (folder / f"{pid}.catalog.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    (folder / f"{pid}.md").write_text("# Manual Import Paper", encoding="utf-8")
    (folder / f"{pid}.pdf").write_bytes(b"%PDF")
    (folder / "images").mkdir()
    return folder


def _commit(tmp_path: Path, folder: Path) -> dict:
    # commit now requires a formalized folder; formalize is the readiness gate.
    from src.services.paper_raw_formalizer import PaperRawFormalizationService

    formalized = PaperRawFormalizationService(
        paper_raw_dir=folder.parent, papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
    ).formalize(folder)
    if not formalized.get("success"):
        return formalized
    return V2PaperCommitService(
        papers_dir=tmp_path / "papers",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
    ).commit_paper_raw(formalized["folder"])


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda m: m["identifiers"].update({"doi": ""}), "metadata.identifiers.doi is required"),
        (lambda m: m["title"].update({"original": ""}), "metadata.title.original is required"),
        (lambda m: m.update({"authors": []}), "metadata.authors must contain at least one author"),
        (lambda m: m.update({"year": ""}), "metadata.year is required"),
        (lambda m: m["container"].update({"journal": "", "conference": "", "booktitle": ""}), "metadata.container.journal"),
    ],
)
def test_incomplete_manual_import_metadata_cannot_commit(tmp_path, mutate, expected):
    folder = _raw_folder(tmp_path)
    meta_path = folder / f"{folder.name}.metadata.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    mutate(metadata)
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")

    result = _commit(tmp_path, folder)

    assert result["success"] is False
    assert result["status"] == "metadata_incomplete"
    assert any(expected in err for err in result["errors"])
    assert not (tmp_path / "papers" / folder.name).exists()


def test_catalog_draft_does_not_bypass_metadata_gate(tmp_path):
    folder = _raw_folder(tmp_path)
    meta_path = folder / f"{folder.name}.metadata.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    metadata["identifiers"]["doi"] = ""
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    assert (folder / f"{folder.name}.catalog.json").exists()

    result = _commit(tmp_path, folder)

    assert result["success"] is False
    assert result["status"] == "metadata_incomplete"


def test_commit_rejects_paper_id_mismatch(tmp_path):
    folder = _raw_folder(tmp_path)
    meta_path = folder / f"{folder.name}.metadata.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    metadata["title"]["short_zh"] = "不同名称"
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")

    result = _commit(tmp_path, folder)

    assert result["success"] is False
    assert result["status"] == "paper_id_mismatch"
    assert any("paper_id mismatch" in err for err in result["errors"])
    assert not (tmp_path / "papers" / folder.name).exists()


def test_catalog_invalid_type_cannot_commit(tmp_path):
    folder = _raw_folder(tmp_path)
    catalog_path = folder / f"{folder.name}.catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["evidence_profile"]["important_tables"] = {"table": "not a list"}
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")

    result = _commit(tmp_path, folder)

    assert result["success"] is False
    assert result["status"] == "catalog_invalid"
    assert any("important_tables must be a list" in err for err in result["errors"])


def test_all_ready_requires_ready_for_commit_status(tmp_path):
    raw = tmp_path / "paper_raw"
    ledger_path = tmp_path / "catalog" / "paper_number_ledger.json"
    folder = _raw_folder(tmp_path, "2024_wang_状态门禁")
    (folder / ".import_status.json").write_text(json.dumps({"status": "metadata_incomplete"}), encoding="utf-8")

    assert folder not in _ready_dirs(raw, ledger_path)

    # commit also requires formalize outputs (formalization.json + paper.number marker)
    (folder / ".import_status.json").write_text(json.dumps({"status": "ready_for_commit"}), encoding="utf-8")
    assert folder not in _ready_dirs(raw, ledger_path)  # still missing formalization.json + marker

    (folder / f"{folder.name}.formalization.json").write_text("{}", encoding="utf-8")
    (folder / "0000000000000001.paper.number").write_text("{}", encoding="utf-8")
    assert folder in _ready_dirs(raw, ledger_path)


def test_manual_confirmed_complete_metadata_can_commit(tmp_path):
    folder = _raw_folder(tmp_path)

    result = _commit(tmp_path, folder)

    assert result["success"] is True
    assert result["status"] == "imported"
    assert (tmp_path / "papers" / "2024_wang_手动导入").exists()


def test_duplicate_doi_cannot_commit(tmp_path):
    first = _raw_folder(tmp_path, "2024_wang_第一篇")
    second = _raw_folder(tmp_path, "2024_wang_第二篇")
    _commit(tmp_path, first)

    result = _commit(tmp_path, second)

    assert result["success"] is False
    assert result["status"] == "possible_duplicate"
    assert any("duplicate DOI" in err for err in result["errors"])
