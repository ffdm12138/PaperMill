from __future__ import annotations

import json
from pathlib import Path

from scripts.audit_metadata_quality import main as audit_main
from scripts.pack_repo import _should_pack
from src.metadata.quality import audit_metadata_library, metadata_quality_hard_errors
from tests.factories.metadata_factory import make_minimal_metadata


ROOT = Path(__file__).resolve().parents[2]


def _receipt(status: str = "matched") -> dict:
    return {"match_status": status}


def test_metadata_quality_uses_independent_receipt():
    metadata = make_minimal_metadata()
    assert metadata_quality_hard_errors(metadata, _receipt()) == []
    assert "metadata_match" not in metadata
    assert any("independent metadata match receipt" in error for error in metadata_quality_hard_errors(metadata))


def test_metadata_quality_checks_bibliographic_facts():
    metadata = make_minimal_metadata()
    metadata["identifiers"]["doi"] = ""
    assert "missing metadata.identifiers.doi" in metadata_quality_hard_errors(metadata, _receipt())


def test_audit_report_reads_formal_receipt(tmp_path):
    paper_name = "2024_Doe_测试"
    folder = tmp_path / "papers" / paper_name
    folder.mkdir(parents=True)
    (folder / f"{paper_name}.metadata.json").write_text(json.dumps(make_minimal_metadata(), ensure_ascii=False), encoding="utf-8")
    (folder / f"{paper_name}.metadata_match.json").write_text(json.dumps(_receipt()), encoding="utf-8")
    report = audit_metadata_library(tmp_path / "papers")
    assert report["errors"] == []


def test_audit_report_strips_full_paper_number_marker_suffix(tmp_path):
    paper_number = "0000000000000001"
    paper_name = "2024_Doe_测试"
    folder = tmp_path / "papers" / paper_name
    folder.mkdir(parents=True)
    (folder / f"{paper_number}.paper.number").write_text("not-json", encoding="utf-8")
    (folder / f"{paper_name}.metadata.json").write_text(
        json.dumps(make_minimal_metadata(), ensure_ascii=False),
        encoding="utf-8",
    )
    (folder / f"{paper_name}.metadata_match.json").write_text(
        json.dumps(_receipt()),
        encoding="utf-8",
    )

    report = audit_metadata_library(tmp_path / "papers")

    assert report["papers"][0]["paper_number"] == paper_number


def test_audit_report_writes_stable_json(tmp_path):
    paper_name = "2024_Doe_测试"
    folder = tmp_path / "papers" / paper_name
    folder.mkdir(parents=True)
    (folder / f"{paper_name}.metadata.json").write_text(json.dumps(make_minimal_metadata(), ensure_ascii=False), encoding="utf-8")
    (folder / f"{paper_name}.metadata_match.json").write_text(json.dumps(_receipt()), encoding="utf-8")
    report_path = tmp_path / "metadata_quality_report.json"
    assert audit_main(["--papers-dir", str(tmp_path / "papers"), "--report", "--report-path", str(report_path)]) == 0
    assert json.loads(report_path.read_text(encoding="utf-8"))["errors"] == []


def test_metadata_quality_report_is_ignored_and_not_packed():
    assert "data/catalog/metadata_quality_report.json" in (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert not _should_pack("data/catalog/metadata_quality_report.json")
