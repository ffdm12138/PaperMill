import json
from pathlib import Path

from scripts.legacy.repair_bad_formal_imports import audit_bad_imports, plan_or_apply_repair
from src.services.v2_library import empty_catalog, empty_metadata


def _formal_bad_import(root: Path, old_id: str = "2024_Wang_Old_English", paper_number: str = "0000000000000042") -> Path:
    folder = root / "papers" / old_id
    folder.mkdir(parents=True)
    metadata = empty_metadata(paper_number)
    metadata["title"]["original"] = "Old English"
    metadata["title"]["short_zh"] = "旧英文条目"
    metadata["title"]["translated_zh"] = "旧英文条目"
    metadata["year"] = 2024
    metadata["authors"] = [{"full_name": "Wang A", "family": "Wang", "given": "A", "orcid": "", "affiliation": ""}]
    metadata["container"]["journal"] = "Test Journal"
    metadata["identifiers"]["doi"] = "10.1/repair"
    metadata["metadata_match"]["status"] = "matched"
    metadata["metadata_match"]["confidence"] = 1.0
    metadata["pdf"] = {"sha256": "", "file_size": 0}
    catalog = empty_catalog()
    catalog["content_identity"]["content_title"] = "旧英文条目"
    catalog["classification"]["primary_domain"] = "snow"
    catalog["screening"]["reason"] = "legacy repair fixture"
    (folder / f"{old_id}.metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    (folder / f"{old_id}.catalog.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    (folder / f"{old_id}.md").write_text("# Old English", encoding="utf-8")
    (folder / f"{old_id}.pdf").write_bytes(b"%PDF-repair")
    (folder / "images").mkdir()
    (folder / f"{paper_number}.metadata.candidates.json").write_text("{}", encoding="utf-8")
    (folder / f"{paper_number}.metadata.resolve_report.json").write_text("{}", encoding="utf-8")
    (folder / f"{paper_number}.paper.number").write_text(
        json.dumps({"paper_number": paper_number, "folder_name": old_id}),
        encoding="utf-8",
    )
    return folder


def _manifest(path: Path, old_id: str = "2024_Wang_Old_English") -> Path:
    data = {
        "items": [{
            "old_paper_id": old_id,
            "paper_number": "0000000000000042",
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
    assert item["paper_number"] == "0000000000000042"
    assert item["resolver_side_file_prefixes"] == ["0000000000000042"]
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
    assert report["items"][0]["status"] == "failed"
    assert any("catalog.content_identity.content_title is legacy" in err for err in report["items"][0]["errors"])
    assert old.exists()
    assert not (tmp_path / "paper_raw" / "0000000000000042").exists()


def test_repair_apply_does_not_bypass_current_schema_gates(tmp_path):
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

    assert report["items"][0]["status"] == "failed"
    assert old.exists()
    assert not (tmp_path / "catalog" / "paper_number_ledger.json").exists()
