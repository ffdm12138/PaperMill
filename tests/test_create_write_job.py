import argparse
import json
from pathlib import Path

import pytest

from scripts.create_write_job import create_write_job


def _metadata(idx: int) -> dict:
    return {
        "citation_key": f"writer{idx}2026",
        "entry_type": "article",
        "title": {"original": f"Writer Paper {idx}"},
        "authors": [{"full_name": f"Author {idx}", "family": f"Author{idx}", "given": "A"}],
        "year": 2026,
        "container": {"journal": "Writer Journal"},
        "publication": {"volume": "1", "number": str(idx), "pages": f"{idx}-{idx + 1}"},
        "identifiers": {"doi": f"10.1234/writer.{idx}"},
        "links": {"url": f"https://example.org/{idx}"},
    }


def _catalog(idx: int, *, topic: str = "blowing snow") -> dict:
    return {
        "schema_version": "2.0",
        "paper_number": f"{idx:016d}",
        "paper_id": f"2026_author{idx}_writer_paper_{idx}",
        "asset_refs": {"markdown": "", "pdf": "", "images_dir": "", "figures": []},
        "content_identity": {"content_title": f"Writer Paper {idx}"},
        "classification": {
            "primary_domain": "writer_domain",
            "secondary_domains": ["writer_domain"],
            "topic_tags": [topic],
            "methods_tags": [],
            "phenomena_tags": [],
            "material_tags": [],
            "model_tags": [],
        },
        "screening": {"read_decision": "must_read", "relevance_score": idx},
        "research_card": {
            "study_object": "snow",
            "method_summary": "fixture method",
            "main_findings": ["fixture finding"],
            "limitations": ["fixture limitation"],
        },
        "evidence_profile": {},
        "content_notes": {"short_summary": "fixture summary"},
        "provenance": {},
    }


def _make_library(tmp_path: Path, count: int = 3) -> tuple[Path, Path, Path, list[dict]]:
    papers_dir = tmp_path / "data" / "papers"
    catalog_path = tmp_path / "data" / "catalog" / "all.catalog.json"
    entries = []
    for idx in range(1, count + 1):
        paper_id = f"2026_author{idx}_writer_paper_{idx}"
        folder = papers_dir / paper_id
        (folder / "images").mkdir(parents=True)
        metadata = _metadata(idx)
        catalog = _catalog(idx)
        (folder / f"{paper_id}.metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        (folder / f"{paper_id}.catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
        (folder / f"{paper_id}.md").write_text(f"# Writer Paper {idx}\n", encoding="utf-8")
        (folder / f"{paper_id}.pdf").write_bytes(b"%PDF-fixture")
        entry = dict(catalog)
        entries.append(entry)
    catalog_path.parent.mkdir(parents=True)
    catalog_path.write_text(json.dumps({"schema_version": "2.0", "papers": entries}), encoding="utf-8")
    return catalog_path, papers_dir, tmp_path / "write" / "jobs", entries


def _args(catalog_path: Path, papers_dir: Path, write_dir: Path, **kwargs) -> argparse.Namespace:
    defaults = {
        "job_id": "job_writer",
        "paper_numbers": None,
        "limit": None,
        "primary_domain": None,
        "topic": None,
        "read_decision": None,
        "min_relevance_score": None,
        "overwrite": False,
        "all_catalog": catalog_path,
        "papers_dir": papers_dir,
        "write_dir": write_dir,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_create_write_job_from_fixture_catalog(tmp_path):
    catalog_path, papers_dir, write_dir, entries = _make_library(tmp_path)

    result = create_write_job(_args(
        catalog_path,
        papers_dir,
        write_dir,
        paper_numbers=[entry["paper_number"] for entry in entries[:2]],
    ))

    job_dir = write_dir / "job_writer"
    assert result["status"] == "prepared"
    assert result["quality_status"] == "not_accepted"
    assert result["selected_count"] == 2
    assert (job_dir / "README.md").exists()
    assert (job_dir / "reports" / "selected_papers.md").exists()
    assert (job_dir / "article" / entries[0]["paper_number"]).exists()
    assert not (job_dir / "tex").exists()


def test_create_write_job_supports_catalog_filters(tmp_path):
    catalog_path, papers_dir, write_dir, _ = _make_library(tmp_path)

    result = create_write_job(_args(
        catalog_path,
        papers_dir,
        write_dir,
        job_id="filter_job",
        topic="blowing",
        primary_domain="writer_domain",
        read_decision="must_read",
        min_relevance_score=2,
        limit=1,
    ))

    assert result["selected_count"] == 1
    selected = json.loads((write_dir / "filter_job" / "selected_catalog.json").read_text(encoding="utf-8"))
    assert selected["papers"][0]["paper_number"] == "0000000000000003"


def test_create_write_job_missing_catalog_does_not_create_job(tmp_path):
    _, papers_dir, write_dir, _ = _make_library(tmp_path)
    missing_catalog = tmp_path / "missing" / "all.catalog.json"

    with pytest.raises(FileNotFoundError):
        create_write_job(_args(missing_catalog, papers_dir, write_dir))

    assert not (write_dir / "job_writer").exists()
