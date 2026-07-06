import pytest
pytestmark = pytest.mark.legacy
import json
from pathlib import Path

from scripts.legacy.audit_paper_raw_formal_imports import audit_formal_imports
from scripts.validate_v2_library import validate_v2_library
from src.services.v2_library import empty_metadata


def _formal_bad_catalog(root: Path, pid: str = "2024_wang_bad_import", source_id: str = "000001") -> Path:
    folder = root / "papers" / pid
    folder.mkdir(parents=True)
    metadata = empty_metadata(source_id)
    metadata["title"]["original"] = "Bad Import"
    metadata["title"]["short_zh"] = "bad_import"
    metadata["year"] = 2024
    metadata["authors"] = [{"full_name": "wang A", "family": "wang", "given": "A", "orcid": "", "affiliation": ""}]
    metadata["container"]["journal"] = "Test Journal"
    metadata["identifiers"]["doi"] = "10.1/bad-import"
    metadata["metadata_match"]["status"] = "matched"
    metadata["metadata_match"]["confidence"] = 1.0
    metadata["pdf"] = {"sha256": "", "file_size": 0}
    (folder / f"{pid}.metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (folder / f"{pid}.catalog.json").write_text('{"schema_version": "2.0", "bad": ', encoding="utf-8")
    (folder / f"{pid}.md").write_text("# Bad Import", encoding="utf-8")
    (folder / f"{pid}.pdf").write_bytes(b"%PDF-bad")
    (folder / "images").mkdir()
    return folder


def _raw_backing(root: Path, source_id: str = "000001") -> Path:
    folder = root / "paper_raw" / source_id
    folder.mkdir(parents=True)
    (folder / f"{source_id}.pdf").write_bytes(b"%PDF-bad")
    (folder / f"{source_id}.md").write_text("# Bad Import", encoding="utf-8")
    (folder / f"{source_id}.metadata.json").write_text(json.dumps(empty_metadata(source_id)), encoding="utf-8")
    (folder / "images").mkdir()
    return folder


def test_audit_marks_bad_formal_copy_safe_to_delete_when_raw_backing_exists(tmp_path):
    formal = _formal_bad_catalog(tmp_path)
    raw = _raw_backing(tmp_path)

    report = audit_formal_imports(
        papers_dir=tmp_path / "papers",
        paper_raw_dir=tmp_path / "paper_raw",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
    )

    item = report["items"][0]
    assert item["delete_decision"] == "safe_to_delete"
    assert item["raw_backing"]["found"] is True
    assert formal.exists()
    assert raw.exists()


def test_audit_delete_safe_removes_only_raw_backed_bad_formal_copy(tmp_path):
    formal = _formal_bad_catalog(tmp_path)
    raw = _raw_backing(tmp_path)

    report = audit_formal_imports(
        papers_dir=tmp_path / "papers",
        paper_raw_dir=tmp_path / "paper_raw",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        apply=True,
        delete_safe=True,
    )

    assert report["deleted"] == [formal.name]
    assert not formal.exists()
    assert raw.exists()
    assert (tmp_path / "catalog" / "all.catalog.json").exists()


def test_audit_does_not_delete_bad_formal_copy_without_raw_backing(tmp_path):
    formal = _formal_bad_catalog(tmp_path)

    report = audit_formal_imports(
        papers_dir=tmp_path / "papers",
        paper_raw_dir=tmp_path / "paper_raw",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        apply=True,
        delete_safe=True,
    )

    item = report["items"][0]
    assert item["delete_decision"] == "unsafe_delete_requires_confirmation"
    assert item["deleted"] is False
    assert formal.exists()


def test_validate_v2_library_reports_malformed_catalog_without_crashing(tmp_path):
    formal = _formal_bad_catalog(tmp_path)

    errors, _ = validate_v2_library(
        papers_dir=tmp_path / "papers",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        check_paths=False,
    )

    assert any(formal.name in err and "invalid JSON" in err for err in errors)


def _formal_state_bad(root: Path, pid: str = "2024_wang_状态审计") -> Path:
    """A formal folder missing marker + with transient files + unmatched metadata."""
    folder = root / "papers" / pid
    folder.mkdir(parents=True)
    metadata = empty_metadata("000001")
    metadata["title"]["original"] = "State Audit"
    metadata["title"]["short_zh"] = "状态审计"
    metadata["title"]["translated_zh"] = "状态审计"
    metadata["year"] = 2024
    metadata["authors"] = [{"full_name": "wang A", "family": "wang", "given": "A", "orcid": "", "affiliation": ""}]
    metadata["container"]["journal"] = "Test Journal"
    metadata["identifiers"]["doi"] = "10.1/state"
    metadata["metadata_match"]["status"] = "unmatched"  # bad: not matched
    metadata["pdf"] = {"sha256": "", "file_size": 0}
    (folder / f"{pid}.metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    catalog = {
        "schema_version": "2.0", "paper_id": pid, "paper_number": "0000000000000001",
        "source_id": "000001",
        "asset_refs": {"markdown": f"{pid}.md", "pdf": f"{pid}.pdf",
                       "metadata": f"{pid}.metadata.json", "catalog": f"{pid}.catalog.json",
                       "images_dir": "images/", "figures": []},
        "content_identity": {"content_title": "状态审计", "md_title_candidates": [],
                             "content_language": "zh", "document_type": "article"},
        "classification": {"primary_domain": "test", "secondary_domains": [], "topic_tags": ["test"],
                           "methodology_tags": [], "evidence_tags": [], "audience_tags": [], "stage_tags": []},
        "screening": {"read_decision": "pending", "relevance_score": 5, "confidence_score": 5, "reason": "测试。"},
        "research_card": {"research_problem": "研究状态审计。", "core_question": "如何审计？",
                          "hypothesis_or_objective": "验证审计。", "study_object": "审计",
                          "method_summary": "审计方法。", "data_or_experiment": "审计数据。",
                          "main_findings": ["审计"], "mechanisms": ["审计"], "limitations": ["审计"],
                          "usefulness_for_user": "审计"},
        "evidence_profile": {"important_tables": [], "important_figures": [], "key_equations": [],
                             "datasets": [], "code_refs": [], "definitions": []},
        "content_notes": {"short_summary": "状态审计测试。", "possible_use_in_writing": [],
                          "open_questions": [], "follow_up_tasks": []},
        "provenance": {"generated_from": "manual", "generator_version": "test", "generated_at": "2026-01-01"},
    }
    (folder / f"{pid}.catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    (folder / f"{pid}.md").write_text("# State Audit", encoding="utf-8")
    (folder / f"{pid}.pdf").write_bytes(b"%PDF-state")
    (folder / "images").mkdir()
    # NO .paper.number marker (bad)
    # transient file present (bad)
    (folder / "stage_manifest.json").write_text("{}", encoding="utf-8")
    return folder


def test_audit_reports_formal_state_errors(tmp_path):
    _formal_state_bad(tmp_path)
    report = audit_formal_imports(
        papers_dir=tmp_path / "papers",
        paper_raw_dir=tmp_path / "paper_raw",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
    )
    item = report["items"][0]
    assert item["status"] == "bad"
    errors_joined = " ".join(item["errors"])
    assert "marker" in errors_joined
    assert "metadata_match.status" in errors_joined
    assert "transient file" in errors_joined
    assert item["quarantine_decision"] == "quarantine"


def test_audit_quarantine_apply_moves_bad_entry(tmp_path):
    formal = _formal_state_bad(tmp_path)
    report = audit_formal_imports(
        papers_dir=tmp_path / "papers",
        paper_raw_dir=tmp_path / "paper_raw",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        apply=True,
        quarantine=True,
    )
    assert report["quarantined_count"] == 1
    assert not formal.exists()
    assert (tmp_path / "papers_quarantine").exists()
    assert report["rebuilt_catalog_indexes"] is True
