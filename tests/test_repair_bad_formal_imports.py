import json
from pathlib import Path

from scripts.repair_bad_formal_imports import audit_bad_imports, plan_or_apply_repair
from src.services.v2_library import empty_catalog, empty_metadata


def _formal_bad_import(root: Path, old_id: str = "2024_Wang_Old_English", source_id: str = "000001") -> Path:
    folder = root / "papers" / old_id
    folder.mkdir(parents=True)
    metadata = empty_metadata(source_id)
    metadata["title"]["original"] = "Old English"
    metadata["title"]["short_zh"] = "旧英文条目"
    metadata["title"]["translated_zh"] = "旧英文条目"
    metadata["year"] = 2024
    metadata["authors"] = [{"full_name": "Wang A", "family": "Wang", "given": "A", "orcid": "", "affiliation": ""}]
    metadata["container"]["journal"] = "Test Journal"
    metadata["identifiers"]["doi"] = "10.1/repair"
    metadata["metadata_match"]["status"] = "matched"
    metadata["metadata_match"]["confidence"] = 1.0
    metadata["pdf"]["sha256"] = ""
    metadata["pdf"]["file_size"] = 0
    catalog = empty_catalog()
    catalog["content_identity"]["content_title"] = "旧英文条目"
    catalog["classification"]["primary_domain"] = "snow"
    catalog["screening"]["reason"] = "该文献需要退回 raw 后重新入库。"
    catalog["research_card"].update({
        "research_problem": "研究错误正式入库修复。",
        "core_question": "如何从正式库退回 paper_raw？",
        "hypothesis_or_objective": "验证修复工具保留编号。",
        "study_object": "错误入库条目",
        "method_summary": "使用本地 mock 资产执行修复。",
        "data_or_experiment": "临时正式库与 paper_raw 目录。",
        "main_findings": ["修复后目录使用中文短题。"],
        "mechanisms": ["先 quarantine，再重建 paper_raw。"],
        "limitations": ["仅覆盖结构性修复。"],
        "usefulness_for_user": "用于恢复正式库卫生。",
    })
    catalog["content_notes"]["short_summary"] = "错误入库修复测试。"
    (folder / f"{old_id}.metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    (folder / f"{old_id}.catalog.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    (folder / f"{old_id}.md").write_text("# Old English", encoding="utf-8")
    (folder / f"{old_id}.pdf").write_bytes(b"%PDF-repair")
    (folder / "images").mkdir()
    (folder / f"{source_id}.metadata.candidates.json").write_text("{}", encoding="utf-8")
    (folder / f"{source_id}.metadata.resolve_report.json").write_text("{}", encoding="utf-8")
    (folder / "0000000000000042.paper.number").write_text(
        json.dumps({"paper_number": "0000000000000042", "folder_name": old_id}),
        encoding="utf-8",
    )
    return folder


def _manifest(path: Path, old_id: str = "2024_Wang_Old_English") -> Path:
    data = {
        "items": [{
            "old_paper_id": old_id,
            "source_id": "000001",
            "old_paper_number": "0000000000000042",
            "short_zh": "中文修复条目",
            "translated_zh": "中文修复条目",
            "confirmed": True,
            "allow_reimport": True,
            "catalog_path": "",
        }]
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_repair_audit_emits_manifest_template(tmp_path):
    _formal_bad_import(tmp_path)

    report = audit_bad_imports(tmp_path / "papers")

    assert len(report["items"]) == 1
    item = report["items"][0]
    assert item["old_paper_id"] == "2024_Wang_Old_English"
    assert item["source_id"] == "000001"
    assert item["old_paper_number"] == "0000000000000042"
    assert item["confirmed"] is False


def test_repair_dry_run_does_not_mutate(tmp_path):
    old = _formal_bad_import(tmp_path)
    manifest = _manifest(tmp_path / "manifest.json")

    report = plan_or_apply_repair(
        manifest_path=manifest,
        papers_dir=tmp_path / "papers",
        paper_raw_dir=tmp_path / "paper_raw",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        apply=False,
    )

    assert report["applied"] is False
    assert report["items"][0]["new_paper_id"] == "2024_Wang_中文修复条目"
    assert old.exists()
    assert not (tmp_path / "paper_raw" / "000001").exists()


def test_repair_apply_quarantines_rebuilds_and_preserves_number(tmp_path):
    old = _formal_bad_import(tmp_path)
    manifest = _manifest(tmp_path / "manifest.json")

    report = plan_or_apply_repair(
        manifest_path=manifest,
        papers_dir=tmp_path / "papers",
        paper_raw_dir=tmp_path / "paper_raw",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        apply=True,
    )

    new_dir = tmp_path / "papers" / "2024_Wang_中文修复条目"
    ledger = json.loads((tmp_path / "catalog" / "paper_number_ledger.json").read_text(encoding="utf-8"))
    assert report["items"][0]["status"] == "imported"
    assert not old.exists()
    assert new_dir.exists()
    assert (new_dir / "0000000000000042.paper.number").exists()
    assert not list(new_dir.glob("*.metadata.candidates.json"))
    assert not list(new_dir.glob("*.metadata.resolve_report.json"))
    assert ledger["items"]["0000000000000042"]["folder_name"] == "2024_Wang_中文修复条目"
    assert (tmp_path / "papers" / "quarantine").exists()
