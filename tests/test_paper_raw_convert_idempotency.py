from __future__ import annotations

import json
import sys
from pathlib import Path

import scripts.convert_paper_raw_batch as batch
from scripts.preflight_paper_raw_import import preflight_one
from src.services.v2_library import PaperRawConverter, V2PaperCommitService, empty_catalog, empty_metadata


class FakeMinerUConverter:
    def __init__(self):
        self.calls: list[str] = []

    def convert(self, pdf, output_root, backend, method, lang, effort, paper_id=""):
        self.calls.append(paper_id)
        out = Path(output_root) / paper_id / "hybrid_auto"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{paper_id}.md").write_text(f"# Converted {paper_id}", encoding="utf-8")
        images = out / "images"
        images.mkdir()
        (images / "new.png").write_bytes(b"new")
        return {
            "success": True,
            "output_dir": str(out),
            "runner": "test",
            "backend": backend,
            "method": method,
            "effort": effort,
        }


class FakeCleaner:
    def locate_markdown(self, source_dir, **kwargs):
        source_dir = Path(source_dir)
        return next(source_dir.glob("*.md"), None)

    def locate_images_dir(self, source_dir, md_path):
        images = Path(source_dir) / "images"
        return images if images.exists() else None


def _raw_folder(root: Path, source_id: str = "000001") -> Path:
    folder = root / source_id
    folder.mkdir(parents=True)
    metadata = empty_metadata(source_id)
    metadata["title"]["original"] = "Idempotent Paper"
    metadata["year"] = 2024
    metadata["authors"] = [{"full_name": "Wang A", "family": "Wang", "given": "A", "orcid": "", "affiliation": ""}]
    metadata["container"]["journal"] = "Test Journal"
    metadata["identifiers"]["doi"] = f"10.1000/{source_id}"
    metadata["metadata_match"]["status"] = "matched"
    (folder / f"{source_id}.metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (folder / f"{source_id}.pdf").write_bytes(b"%PDF-" + source_id.encode("ascii"))
    return folder


def _converter(root: Path, fake: FakeMinerUConverter | None = None) -> tuple[PaperRawConverter, FakeMinerUConverter]:
    fake = fake or FakeMinerUConverter()
    return PaperRawConverter(root, converter=fake, cleaner=FakeCleaner()), fake


def test_repeated_conversion_writes_manifest_then_skips(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    _raw_folder(paper_raw)
    converter, fake = _converter(paper_raw)

    first = converter.convert("000001")
    second = converter.convert("000001")

    folder = paper_raw / "000001"
    manifest = json.loads((folder / "000001.conversion.json").read_text(encoding="utf-8"))
    status = json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))
    assert first["success"] is True
    assert second["skipped"] is True
    assert second["status"] == "skipped_existing"
    assert fake.calls == ["000001"]
    assert manifest["status"] == "converted"
    assert manifest["markdown_sha256"]
    assert status["status"] == "converted"


def test_legacy_converted_assets_skip_without_manifest(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    folder = _raw_folder(paper_raw)
    (folder / "000001.md").write_text("# Legacy", encoding="utf-8")
    (folder / "images").mkdir()
    converter, fake = _converter(paper_raw)

    result = converter.convert("000001")

    assert result["skipped"] is True
    assert result["conversion_state"] == "converted_legacy"
    assert "legacy" in result["reason"] or "without conversion manifest" in result["reason"]
    assert fake.calls == []


def test_stale_manifest_requires_force_and_force_cleans_old_images(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    folder = _raw_folder(paper_raw)
    (folder / "000001.md").write_text("# Old", encoding="utf-8")
    images = folder / "images"
    images.mkdir()
    (images / "old.png").write_bytes(b"old")
    (folder / "000001.conversion.json").write_text(json.dumps({
        "status": "converted",
        "source_id": "000001",
        "pdf_sha256": "stale",
        "backend": "hybrid-engine",
        "method": "auto",
        "lang": "ch",
        "effort": "medium",
    }), encoding="utf-8")
    converter, fake = _converter(paper_raw)

    stale = converter.convert("000001")
    forced = converter.convert("000001", force_reconvert=True)

    assert stale["success"] is False
    assert stale["status"] == "stale_conversion"
    assert forced["success"] is True
    assert fake.calls == ["000001"]
    assert not (images / "old.png").exists()
    assert (images / "new.png").exists()


def test_partial_assets_require_force(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    folder = _raw_folder(paper_raw)
    (folder / "000001.md").write_text("# Partial", encoding="utf-8")
    converter, fake = _converter(paper_raw)

    partial = converter.convert("000001")
    forced = converter.convert("000001", force_reconvert=True)

    assert partial["success"] is False
    assert partial["status"] == "partial_conversion"
    assert forced["success"] is True
    assert fake.calls == ["000001"]


def test_all_skipped_batch_does_not_run_runtime_preflight(monkeypatch, tmp_path):
    paper_raw = tmp_path / "paper_raw"
    folder = _raw_folder(paper_raw)
    (folder / "000001.md").write_text("# Done", encoding="utf-8")
    (folder / "images").mkdir()
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "false")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("runtime preflight should not run when all sources are skipped")

    monkeypatch.setattr("src.mineru_runtime.preflight_gpu", fail_if_called)
    monkeypatch.setattr("src.mineru_runtime.preflight_torch_cuda", fail_if_called)
    saved = sys.argv
    sys.argv = ["convert_paper_raw_batch.py", "--paper-raw-dir", str(paper_raw), "--all", "--apply"]
    try:
        rc = batch.main()
    finally:
        sys.argv = saved

    assert rc == 0


def test_preflight_marks_existing_conversion_as_converted(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    folder = _raw_folder(paper_raw)
    (folder / "000001.md").write_text("# Done", encoding="utf-8")
    (folder / "images").mkdir()

    item = preflight_one(
        paper_raw,
        "000001",
        papers_dir=tmp_path / "papers",
        formal_dois=set(),
        formal_shas=set(),
        raw_doi_counts={"10.1000/000001": 1},
        raw_sha_counts={},
    )

    assert item["status"] == "converted"
    assert item["has_markdown"] is True
    assert item["has_images_dir"] is True


def test_formal_commit_removes_conversion_manifest(tmp_path):
    pid = "2024_wang_转换清单"
    raw = tmp_path / "paper_raw" / pid
    raw.mkdir(parents=True)
    metadata = empty_metadata(pid)
    metadata["title"]["original"] = "Manifest Paper"
    metadata["title"]["translated_zh"] = "转换清单"
    metadata["title"]["short_zh"] = "转换清单"
    metadata["year"] = 2024
    metadata["authors"] = [{"full_name": "wang A", "family": "wang", "given": "A", "orcid": "", "affiliation": ""}]
    metadata["container"]["journal"] = "Test Journal"
    metadata["identifiers"]["doi"] = "10.1000/manifest"
    metadata["metadata_match"]["status"] = "matched"
    catalog = empty_catalog()
    catalog["content_identity"]["content_title"] = "转换清单"
    catalog["classification"]["primary_domain"] = "blowing_snow_physics"
    catalog["screening"]["reason"] = "该文献用于测试转换清单清理。"
    catalog["research_card"].update({
        "research_problem": "研究转换清单清理。",
        "core_question": "正式入库是否移除 conversion manifest？",
        "hypothesis_or_objective": "验证 transient artifact 不进入正式库。",
        "study_object": "转换清单文件",
        "method_summary": "使用 mock paper_raw 执行 commit。",
        "data_or_experiment": "临时 PDF 与 Markdown。",
        "main_findings": ["转换清单会被清理。"],
        "mechanisms": ["commit staging 阶段删除 transient 文件。"],
        "limitations": ["仅覆盖文件清理。"],
        "usefulness_for_user": "保障正式库目录干净。",
    })
    catalog["content_notes"]["short_summary"] = "转换清单清理测试。"
    (raw / f"{pid}.metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    (raw / f"{pid}.catalog.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    (raw / f"{pid}.md").write_text("# Manifest", encoding="utf-8")
    (raw / f"{pid}.pdf").write_bytes(b"%PDF")
    (raw / "images").mkdir()
    (raw / f"{pid}.conversion.json").write_text("{}", encoding="utf-8")

    from src.services.paper_raw_formalizer import PaperRawFormalizationService

    formalized = PaperRawFormalizationService(
        paper_raw_dir=raw.parent, papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
    ).formalize(raw)
    assert formalized.get("success"), formalized

    result = V2PaperCommitService(
        papers_dir=tmp_path / "papers",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
    ).commit_paper_raw(formalized["folder"])

    assert result["success"] is True
    assert not (tmp_path / "papers" / pid / f"{pid}.conversion.json").exists()
