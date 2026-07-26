"""Integration tests for scripts/export_write_job_bib.py (synthetic jobs only)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from scripts.export_write_job_bib import export_job_references
from src.writer.bib import bib_key_for_entry, parse_blocks

pytestmark = pytest.mark.integration


def _metadata(number: int, family: str, year: int) -> dict:
    return {
        "title": {"original": f"Synthetic saltation study {number}"},
        "authors": [{"full_name": f"A {family}", "family": family, "given": "A"}],
        "year": year,
        "container": {"journal": "Journal of Synthetic Tests", "publisher": "Press"},
        "publication": {"volume": "1", "number": "2", "issue": "2", "pages": "1-10",
                        "article_number": ""},
        "identifiers": {"doi": f"10.9000/synthetic.{number}"},
        "links": {"url": f"https://doi.org/10.9000/synthetic.{number}"},
    }


def _make_job(tmp_path: Path, job_id: str = "001_review_demo_abc123",
              paper_count: int = 3) -> tuple[Path, list[dict]]:
    job_dir = tmp_path / "jobs" / job_id
    papers = []
    for n in range(1, paper_count + 1):
        number = f"{n:016d}"
        paper_name = f"200{n}_Author{n}_synthetic"
        metadata = _metadata(n, f"Author{n}", 2000 + n)
        folder = job_dir / "article" / number
        folder.mkdir(parents=True)
        (folder / f"{paper_name}.metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        papers.append({"paper_number": number, "paper_name": paper_name,
                       "metadata": metadata})
    (job_dir / "selected_catalog.json").write_text(json.dumps({
        "schema_version": "1.0", "job_id": job_id, "source_categories": ["测试"],
        "papers": [{"paper_number": p["paper_number"], "paper_name": p["paper_name"]}
                   for p in papers],
    }, ensure_ascii=False), encoding="utf-8")
    (job_dir / "job.json").write_text(json.dumps({
        "schema_version": "1.0", "job_id": job_id, "workflow": "catalog_review",
    }), encoding="utf-8")
    return job_dir, papers


def _args(tmp_path: Path, job_id: str, paper_numbers: list[str] | None = None):
    return argparse.Namespace(job_id=job_id, write_dir=tmp_path / "jobs",
                              paper_numbers=paper_numbers)


def test_export_writes_bib_with_metadata_derived_keys(tmp_path: Path):
    job_dir, papers = _make_job(tmp_path)
    result = export_job_references(_args(tmp_path, job_dir.name))
    assert result["passed"] is True
    assert result["count"] == 3
    bib_text = (job_dir / "tex" / "references.bib").read_text(encoding="utf-8")
    blocks = parse_blocks(bib_text)
    for paper in papers:
        expected_key = bib_key_for_entry(
            {"paper_name": paper["paper_name"], "metadata": paper["metadata"]})
        assert expected_key in blocks
        assert f"10.9000/synthetic." in blocks[expected_key]


def test_export_fails_closed_on_missing_doi(tmp_path: Path):
    job_dir, papers = _make_job(tmp_path)
    broken = papers[1]
    folder = job_dir / "article" / broken["paper_number"]
    metadata = dict(broken["metadata"])
    metadata["identifiers"] = {"doi": ""}
    (folder / f"{broken['paper_name']}.metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False), encoding="utf-8")

    result = export_job_references(_args(tmp_path, job_dir.name))
    assert result["passed"] is False
    assert any(broken["paper_number"] in err for err in result["errors"])
    assert not (job_dir / "tex" / "references.bib").exists()


def test_export_subset_by_paper_numbers(tmp_path: Path):
    job_dir, papers = _make_job(tmp_path)
    only = papers[0]["paper_number"]
    result = export_job_references(_args(tmp_path, job_dir.name, [only]))
    assert result["passed"] is True
    assert result["count"] == 1
    assert result["entries"][0]["paper_number"] == only


def test_export_fails_on_unknown_requested_paper_number(tmp_path: Path):
    job_dir, papers = _make_job(tmp_path)
    known = papers[0]["paper_number"]
    ghost = "0000000000000099"
    result = export_job_references(_args(tmp_path, job_dir.name, [known, ghost]))
    assert result["passed"] is False
    assert any(ghost in err for err in result["errors"])
    assert not (job_dir / "tex" / "references.bib").exists()


def test_export_fails_on_multiple_metadata_files(tmp_path: Path):
    job_dir, papers = _make_job(tmp_path)
    ambiguous = papers[1]
    folder = job_dir / "article" / ambiguous["paper_number"]
    (folder / "0000_Other_duplicate.metadata.json").write_text(
        json.dumps(ambiguous["metadata"], ensure_ascii=False), encoding="utf-8")

    result = export_job_references(_args(tmp_path, job_dir.name))
    assert result["passed"] is False
    assert any("multiple" in err and ambiguous["paper_number"] in err
               for err in result["errors"])
    assert not (job_dir / "tex" / "references.bib").exists()


def test_export_reports_missing_selected_catalog_as_error(tmp_path: Path):
    job_dir, _ = _make_job(tmp_path)
    (job_dir / "selected_catalog.json").unlink()
    result = export_job_references(_args(tmp_path, job_dir.name))
    assert result["passed"] is False
    assert any("selected_catalog.json" in err for err in result["errors"])
