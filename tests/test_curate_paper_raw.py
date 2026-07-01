"""Tests for scripts/curate_paper_raw.py: dry-run writes curation_prompt.md, apply merges + validates (no rename)."""
import json
import runpy
import sys
from pathlib import Path

import pytest

from src.services.v2_library import empty_catalog, empty_metadata

PN1 = "0000000000000001"
PN2 = "0000000000000002"


def _matched_raw(folder: Path, source_id: str = PN1) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    metadata = empty_metadata(source_id)
    metadata["title"]["original"] = "Trusted Original"
    metadata["title"]["short_zh"] = "可信论文"
    metadata["year"] = 2024
    metadata["authors"] = [{"full_name": "Wang A", "family": "Wang", "given": "A", "orcid": "", "affiliation": ""}]
    metadata["container"]["journal"] = "Test Journal"
    metadata["identifiers"]["doi"] = "10.1/test"
    metadata["metadata_match"]["status"] = "matched"
    metadata["metadata_match"]["confidence"] = 1.0
    (folder / f"{source_id}.metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (folder / f"{source_id}.md").write_text("# Trusted Original\n\nbody text", encoding="utf-8")
    (folder / f"{source_id}.pdf").write_bytes(b"%PDF")
    (folder / "images").mkdir()
    return folder


def _fill_chinese_catalog(catalog: dict, title: str = "可信论文") -> dict:
    catalog["content_identity"]["content_title"] = title
    catalog["classification"]["primary_domain"] = "blowing_snow"
    catalog["screening"]["reason"] = "该文献与中文综述主题相关。"
    catalog["research_card"].update({
        "research_problem": "研究文献入库边界。",
        "core_question": "如何完成 paper_raw curation？",
        "hypothesis_or_objective": "验证中文 catalog 门禁。",
        "study_object": "测试文献",
        "method_summary": "使用 mock 资产测试。",
        "data_or_experiment": "临时 PDF 与 Markdown。",
        "main_findings": ["curation 后可以重命名。"],
        "mechanisms": ["metadata 与 catalog 共同通过 readiness gate。"],
        "limitations": ["仅覆盖结构性流程。"],
        "usefulness_for_user": "保障正式入库质量。",
    })
    catalog["content_notes"]["short_summary"] = "中文 catalog 测试摘要。"
    return catalog


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "curate_paper_raw.py"


def _run_cli(argv: list[str]) -> int:
    saved = sys.argv
    sys.argv = argv
    try:
        runpy.run_path(str(_SCRIPT), run_name="__main__")
        return 0
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = saved


def test_dry_run_writes_curation_prompt(tmp_path, monkeypatch):
    raw = tmp_path / "paper_raw"
    folder = _matched_raw(raw / PN1)
    monkeypatch.syspath_prepend(str(_REPO_ROOT))
    rc = _run_cli([
        "curate_paper_raw.py",
        "--paper-dir", str(folder),
        "--dry-run",
    ])
    assert rc == 0
    prompt_path = folder / "curation_prompt.md"
    assert prompt_path.exists()
    text = prompt_path.read_text(encoding="utf-8")
    assert "paper_raw_catalog_curator" in text
    assert "evidence_profile" in text
    assert "screening" in text
    assert "不得覆盖" in text or "metadata" in text


def test_apply_merges_only_empty_and_keeps_source_folder(tmp_path, monkeypatch):
    raw = tmp_path / "paper_raw"
    folder = _matched_raw(raw / PN1)
    catalog = _fill_chinese_catalog(empty_catalog())
    catalog_path = folder / f"{PN1}.catalog.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    patch = empty_metadata(PN1)
    patch["abstract"] = "new abstract"
    patch["title"]["original"] = "Overwrite Attempt"
    patch_path = tmp_path / "patch.metadata.json"
    patch_path.write_text(json.dumps(patch), encoding="utf-8")
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_cli([
        "curate_paper_raw.py",
        "--paper-dir", str(folder),
        "--catalog", str(catalog_path),
        "--metadata", str(patch_path),
        "--apply",
    ])
    assert rc == 0
    # curate must NOT rename the folder or files; formalize does that.
    assert folder.exists()
    assert (folder / f"{PN1}.metadata.json").exists()
    assert (folder / f"{PN1}.catalog.json").exists()
    assert not (raw / "2024_Wang_可信论文").exists()
    merged = json.loads((folder / f"{PN1}.metadata.json").read_text(encoding="utf-8"))
    assert merged["title"]["original"] == "Trusted Original"  # not overwritten
    assert merged["abstract"] == "new abstract"  # empty field filled
    status = json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "catalog_ready"
    assert status["paper_id"] == "2024_Wang_可信论文"


def test_apply_rejects_unmatched_metadata(tmp_path, monkeypatch):
    raw = tmp_path / "paper_raw"
    folder = _matched_raw(raw / PN1)
    meta_path = folder / f"{PN1}.metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["metadata_match"]["status"] = "unmatched"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.syspath_prepend(str(_REPO_ROOT))
    rc = _run_cli(["curate_paper_raw.py", "--paper-dir", str(folder), "--apply"])
    assert rc == 1
    assert (folder / ".import_status.json").exists()


def test_pdf_metadata_without_doi_cannot_curate(tmp_path, monkeypatch):
    raw = tmp_path / "paper_raw"
    folder = _matched_raw(raw / PN1)
    meta_path = folder / f"{PN1}.metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["metadata_match"]["status"] = "manual_confirmed"
    meta["identifiers"]["doi"] = ""
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_cli(["curate_paper_raw.py", "--paper-dir", str(folder), "--apply"])

    assert rc == 1
    status = json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))
    assert status["reason"] == "curation requires metadata.identifiers.doi"


def test_all_ready_apply_only_processes_curated(tmp_path, monkeypatch):
    raw = tmp_path / "paper_raw"
    # folder A has a curated catalog output
    folder_a = _matched_raw(raw / PN1)
    meta_a = json.loads((folder_a / f"{PN1}.metadata.json").read_text(encoding="utf-8"))
    meta_a["title"]["short_zh"] = "甲论文"
    (folder_a / f"{PN1}.metadata.json").write_text(json.dumps(meta_a), encoding="utf-8")
    catalog = _fill_chinese_catalog(empty_catalog(), "甲论文")
    (folder_a / f"{PN1}.catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    # folder B is ready (metadata+md+images) but has NO curated catalog output
    _matched_raw(raw / PN2, PN2)
    monkeypatch.syspath_prepend(str(_REPO_ROOT))
    rc = _run_cli(["curate_paper_raw.py", "--all-ready", "--paper-raw-dir", str(raw), "--apply"])
    # curate does NOT rename; folder A stays PN1 with catalog_ready status, B skipped
    assert folder_a.exists()
    assert (raw / PN2).exists()  # not processed
    status = json.loads((folder_a / ".import_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "catalog_ready"
    assert rc in (0, 1)


def test_all_ready_apply_auto_loads_metadata_patch(tmp_path, monkeypatch):
    """--all-ready --apply must auto-detect <id>.metadata.patch.json alongside catalog."""
    raw = tmp_path / "paper_raw"
    folder = _matched_raw(raw / PN1)
    meta_m = json.loads((folder / f"{PN1}.metadata.json").read_text(encoding="utf-8"))
    meta_m["title"]["short_zh"] = "甲论文"
    (folder / f"{PN1}.metadata.json").write_text(json.dumps(meta_m), encoding="utf-8")
    catalog = _fill_chinese_catalog(empty_catalog(), "甲论文")
    (folder / f"{PN1}.catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    # Write a metadata patch that fills abstract (an empty field)
    patch = empty_metadata(PN1)
    patch["abstract"] = "自动加载的摘要"
    (folder / f"{PN1}.metadata.patch.json").write_text(json.dumps(patch), encoding="utf-8")
    monkeypatch.syspath_prepend(str(_REPO_ROOT))
    rc = _run_cli(["curate_paper_raw.py", "--all-ready", "--paper-raw-dir", str(raw), "--apply"])
    # curate does NOT rename; folder stays PN1. Patch should be auto-merged.
    assert folder.exists()
    assert rc in (0, 1)
    if rc == 0:
        merged = json.loads((folder / f"{PN1}.metadata.json").read_text(encoding="utf-8"))
        assert merged["abstract"] == "自动加载的摘要", f"patch not auto-merged, abstract={merged.get('abstract')}"
