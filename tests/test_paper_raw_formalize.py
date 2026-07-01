"""Tests for PaperRawFormalizationService + formalize_paper_raw.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.services.paper_raw_formalizer import PaperRawFormalizationService
from src.services.v2_library import (
    PaperRawConverter,
    PaperNumberLedger,
    empty_catalog,
    empty_metadata,
    validate_catalog_schema,
)
from src.services.ingest_state import CATALOG_READY, READY_FOR_COMMIT


def _matched_metadata(source_id: str, *, doi: str = "10.1/test") -> dict:
    metadata = empty_metadata(source_id)
    metadata["title"]["original"] = "Trusted Original"
    metadata["title"]["translated_zh"] = "可信论文"
    metadata["title"]["short_zh"] = "可信论文"
    metadata["year"] = 2024
    metadata["authors"] = [{"full_name": "Wang A", "family": "Wang", "given": "A", "orcid": "", "affiliation": ""}]
    metadata["container"]["journal"] = "Test Journal"
    metadata["identifiers"]["doi"] = doi
    metadata["metadata_match"] = {
        "status": "matched",
        "source": "test",
        "confidence": 1.0,
        "matched_at": "2026-01-01T00:00:00",
        "warnings": [],
        "candidates": [],
    }
    return metadata


def _chinese_catalog() -> dict:
    catalog = empty_catalog()
    catalog["content_identity"]["content_title"] = "可信论文"
    catalog["classification"].update({
        "primary_domain": "blowing_snow_physics",
        "secondary_domains": ["blowing_snow_physics"],
        "topic_tags": ["blowing_snow"],
    })
    catalog["screening"]["reason"] = "该文献与中文综述主题相关。"
    catalog["research_card"].update({
        "research_problem": "测试研究问题",
        "core_question": "测试核心问题",
        "hypothesis_or_objective": "测试目标",
        "study_object": "测试对象",
        "method_summary": "测试方法摘要",
        "data_or_experiment": "测试数据",
        "main_findings": ["测试发现"],
        "mechanisms": ["测试机制"],
        "limitations": ["测试局限"],
        "usefulness_for_user": "测试用途",
    })
    catalog["content_notes"]["short_summary"] = "测试摘要"
    return catalog


def _write_conversion_manifest(folder: Path, source_id: str, pdf_sha: str, md_sha: str) -> None:
    from src.utils.atomic_io import atomic_write_json
    from config.settings import MINERU_BACKEND, MINERU_METHOD, MINERU_LANG, MINERU_EFFORT

    atomic_write_json(folder / f"{source_id}.conversion.json", {
        "schema_version": "1.0",
        "status": "converted",
        "source_id": source_id,
        "pdf_sha256": pdf_sha,
        "pdf_file_size": 4,
        "markdown_path": f"{source_id}.md",
        "markdown_sha256": md_sha,
        "images_dir": "images",
        "images_count": 0,
        "backend": MINERU_BACKEND,
        "method": MINERU_METHOD,
        "lang": MINERU_LANG,
        "effort": MINERU_EFFORT,
        "runner": "",
        "api_url": "",
        "output_dir": str(folder / "output"),
        "converted_at": "2026-01-01T00:00:00",
    }, indent=2)


def _staged_raw(root: Path, source_id: str = "000001", *, doi: str = "10.1/test") -> Path:
    folder = root / "paper_raw" / source_id
    folder.mkdir(parents=True)
    metadata = _matched_metadata(source_id, doi=doi)
    catalog = _chinese_catalog()
    (folder / f"{source_id}.metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (folder / f"{source_id}.catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    (folder / f"{source_id}.md").write_text("# 可信论文\nbody", encoding="utf-8")
    (folder / f"{source_id}.pdf").write_bytes(b"%PDF")
    (folder / "images").mkdir()
    from src.file_fingerprint import compute_sha256

    pdf_sha = compute_sha256(folder / f"{source_id}.pdf")
    import hashlib

    md_sha = hashlib.sha256((folder / f"{source_id}.md").read_bytes()).hexdigest()
    _write_conversion_manifest(folder, source_id, pdf_sha, md_sha)
    return folder


def _service(tmp_path: Path) -> PaperRawFormalizationService:
    return PaperRawFormalizationService(
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
    )


def test_formalize_renames_folder_files_and_reserves_number(tmp_path: Path):
    folder = _staged_raw(tmp_path)
    svc = _service(tmp_path)

    result = svc.formalize(folder)

    assert result["success"]
    assert result["status"] == READY_FOR_COMMIT
    pid = result["paper_id"]
    assert pid == "2024_Wang_可信论文"
    number = result["paper_number"]
    assert number == "0000000000000001"

    renamed = tmp_path / "paper_raw" / pid
    assert renamed.exists()
    assert not folder.exists()
    for suffix in ("metadata.json", "catalog.json", "md", "pdf"):
        assert (renamed / f"{pid}.{suffix}").exists()
    assert (renamed / f"{number}.paper.number").exists()
    assert (renamed / f"{pid}.formalization.json").exists()

    status = json.loads((renamed / ".import_status.json").read_text(encoding="utf-8"))
    assert status["status"] == READY_FOR_COMMIT
    assert status["paper_id"] == pid
    assert status["paper_number"] == number

    data = svc.ledger.load()
    item = data["items"][number]
    assert item["state"] == "reserved"
    assert item["folder_name"] == pid
    assert item["planned_paper_id"] == pid
    assert Path(item["folder_path"]).name == pid
    assert "000001" not in item["folder_path"]
    assert data["max_number"] == number
    marker = json.loads((renamed / f"{number}.paper.number").read_text(encoding="utf-8"))
    assert marker["paper_number"] == number
    assert marker["state"] == "reserved"
    assert marker["folder_name"] == pid
    assert marker["planned_paper_id"] == pid


def test_formalize_backfills_catalog_links(tmp_path: Path):
    folder = _staged_raw(tmp_path)
    svc = _service(tmp_path)
    result = svc.formalize(folder)
    pid = result["paper_id"]
    renamed = tmp_path / "paper_raw" / pid
    catalog = json.loads((renamed / f"{pid}.catalog.json").read_text(encoding="utf-8"))
    assert catalog["paper_id"] == pid
    assert catalog["paper_number"] == result["paper_number"]
    refs = catalog["asset_refs"]
    assert refs["markdown"] == f"{pid}.md"
    assert refs["pdf"] == f"{pid}.pdf"
    assert refs["metadata"] == f"{pid}.metadata.json"
    assert refs["catalog"] == f"{pid}.catalog.json"
    assert refs["images_dir"] == "images/"
    assert validate_catalog_schema(catalog) == []


def test_formalize_reserved_ledger_points_to_renamed_folder(tmp_path: Path):
    folder = _staged_raw(tmp_path, source_id="000001")
    svc = _service(tmp_path)

    result = svc.formalize(folder)

    number = result["paper_number"]
    pid = result["paper_id"]
    item = svc.ledger.load()["items"][number]

    assert item["state"] == "reserved"
    assert item["folder_name"] == pid
    assert item["planned_paper_id"] == pid
    assert Path(item["folder_path"]).name == pid
    assert Path(item["folder_path"]).name != "000001"


def test_formalize_idempotent_on_rerun(tmp_path: Path):
    folder = _staged_raw(tmp_path)
    svc = _service(tmp_path)
    first = svc.formalize(folder)
    renamed = tmp_path / "paper_raw" / first["paper_id"]
    second = svc.formalize(renamed)

    assert second["success"]
    assert second["paper_number"] == first["paper_number"]
    assert second["paper_id"] == first["paper_id"]
    data = svc.ledger.load()
    assert len(data["items"]) == 1  # no new number allocated


def test_formalize_rejects_stale_conversion(tmp_path: Path, monkeypatch):
    folder = _staged_raw(tmp_path)
    svc = _service(tmp_path)

    def _stale(self, folder, *, file_prefix, **kwargs):
        return {"state": "stale", "reason": "PDF sha256 changed", "manifest": None,
                "markdown": "", "images_dir": "", "pdf_sha256": ""}

    monkeypatch.setattr(PaperRawConverter, "inspect_converted_assets", _stale)
    result = svc.formalize(folder)

    assert not result["success"]
    assert result["status"] == "formalize_failed"
    assert folder.exists()  # not renamed
    status = json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "formalize_failed"


def test_formalize_rejects_unmatched_metadata(tmp_path: Path):
    folder = _staged_raw(tmp_path)
    meta_path = folder / "000001.metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["metadata_match"]["status"] = "unmatched"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    svc = _service(tmp_path)

    result = svc.formalize(folder)

    assert not result["success"]
    assert result["status"] == "metadata_unmatched"
    assert folder.exists()
    assert not (tmp_path / "paper_raw" / "2024_Wang_可信论文").exists()
    status = json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "formalize_failed"


def test_formalize_preserve_paper_number(tmp_path: Path):
    folder = _staged_raw(tmp_path)
    svc = _service(tmp_path)
    result = svc.formalize(folder, preserve_paper_number="0000000000000007")
    assert result["paper_number"] == "0000000000000007"
    renamed = tmp_path / "paper_raw" / result["paper_id"]
    assert (renamed / "0000000000000007.paper.number").exists()
    data = svc.ledger.load()
    assert data["max_number"] == "0000000000000007"


def test_formalize_quarantines_duplicate(tmp_path: Path):
    # commit one paper first to seed the formal library
    folder1 = _staged_raw(tmp_path, "000001", doi="10.1/dup")
    svc = _service(tmp_path)
    svc.formalize(folder1)
    # Simulate the formal copy existing with that DOI (duplicate gate target)
    pid1 = "2024_Wang_可信论文"
    papers = tmp_path / "papers" / pid1
    papers.mkdir(parents=True)
    (papers / f"{pid1}.metadata.json").write_text(
        (tmp_path / "paper_raw" / pid1 / f"{pid1}.metadata.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    # second paper_raw with same DOI
    folder2 = _staged_raw(tmp_path, "000002", doi="10.1/dup")
    # its paper_id would collide too; tweak title so paper_id differs but DOI dup
    meta = json.loads((folder2 / "000002.metadata.json").read_text(encoding="utf-8"))
    meta["title"]["short_zh"] = "重复论文"
    meta["title"]["translated_zh"] = "重复论文"
    (folder2 / "000002.metadata.json").write_text(json.dumps(meta), encoding="utf-8")

    result = svc.formalize(folder2)
    assert result["status"] == "possible_duplicate"
    assert Path(result["quarantine_dir"]).exists()
    assert not folder2.exists()


def test_formalize_cli_all_ready_apply(tmp_path: Path):
    folder = _staged_raw(tmp_path)
    # curate would have written catalog_ready; simulate
    from src.services.ingest_state import write_import_status

    write_import_status(folder, CATALOG_READY, reason="curated")
    import subprocess
    import os

    project_root = Path(__file__).resolve().parent.parent
    default_ledger = project_root / "data" / "catalog" / "paper_number_ledger.json"
    before = default_ledger.read_text(encoding="utf-8") if default_ledger.exists() else None

    proc = subprocess.run(
        ["python", "scripts/formalize_paper_raw.py",
         "--all-ready", "--apply",
         "--paper-raw-dir", str(tmp_path / "paper_raw"),
         "--papers-dir", str(tmp_path / "papers"),
         "--ledger-path", str(tmp_path / "catalog" / "paper_number_ledger.json"),
         "--all-catalog-path", str(tmp_path / "catalog" / "all.catalog.json"),
         "--report", str(tmp_path / "report.json")],
        capture_output=True, text=True, cwd=str(project_root),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert (tmp_path / "paper_raw" / "2024_Wang_可信论文" / "2024_Wang_可信论文.formalization.json").exists()
    # tmp ledger must exist and the real default ledger must NOT have been touched.
    assert (tmp_path / "catalog" / "paper_number_ledger.json").exists()
    after = default_ledger.read_text(encoding="utf-8") if default_ledger.exists() else None
    assert after == before, "formalize CLI mutated the real data/catalog/paper_number_ledger.json"
