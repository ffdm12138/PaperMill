"""Integration tests for create_write_job --workflow (prepare step faked)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import scripts.create_write_job as cwj

pytestmark = pytest.mark.integration


def _fake_prepare(write_dir: Path, job_id: str):
    def prepare_workdir(args: argparse.Namespace) -> dict:
        job_dir = write_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "job.json").write_text(json.dumps({
            "schema_version": "1.0", "job_id": job_id,
            "workflow": "catalog_tex_article", "article_dir": "article",
        }), encoding="utf-8")
        return {"job_id": job_id, "selected_count": 3, "papers": []}
    return prepare_workdir


def _args(write_dir: Path, workflow: str) -> argparse.Namespace:
    return argparse.Namespace(
        job_id="001_workflow_demo_abc123", workflow=workflow, paper_numbers=None,
        categories=["测试"], category_mode="union", limit=None, overwrite=False,
        catalog_root=write_dir / "catalog", papers_dir=write_dir / "papers",
        write_dir=write_dir,
    )


@pytest.fixture()
def write_dir(tmp_path: Path, monkeypatch) -> Path:
    write_dir = tmp_path / "jobs"
    monkeypatch.setattr(
        cwj, "prepare_workdir", _fake_prepare(write_dir, "001_workflow_demo_abc123"))
    return write_dir


def test_default_article_workflow_keeps_job_json(write_dir: Path):
    result = cwj.create_write_job(_args(write_dir, "article"))
    assert result["workflow"] == "catalog_tex_article"
    meta = json.loads((write_dir / result["job_id"] / "job.json").read_text(encoding="utf-8"))
    assert meta["workflow"] == "catalog_tex_article"
    readme = (write_dir / result["job_id"] / "README.md").read_text(encoding="utf-8")
    assert "write_catalog_tex_article.py" in readme
    assert not (write_dir / result["job_id"] / "input").exists()


def test_review_workflow_updates_job_json_and_readme(write_dir: Path):
    result = cwj.create_write_job(_args(write_dir, "review"))
    assert result["workflow"] == "catalog_review"
    meta = json.loads((write_dir / result["job_id"] / "job.json").read_text(encoding="utf-8"))
    assert meta["workflow"] == "catalog_review"
    assert meta["article_dir"] == "article"  # other fields preserved
    readme = (write_dir / result["job_id"] / "README.md").read_text(encoding="utf-8")
    assert "export_write_job_bib.py" in readme
    assert "check_write_planning_docs.py" in readme
    assert "catalog_review_writer" in readme


def test_proposal_workflow_writes_research_input_template(write_dir: Path):
    result = cwj.create_write_job(_args(write_dir, "proposal"))
    assert result["workflow"] == "catalog_research_proposal"
    research_input = write_dir / result["job_id"] / "input" / "research_input.md"
    assert research_input.exists()
    text = research_input.read_text(encoding="utf-8")
    assert "（待填）" in text
    assert "研究问题" in text
    # re-running must not clobber a user-edited file
    research_input.write_text("# filled by user", encoding="utf-8")
    cwj.create_write_job(_args(write_dir, "proposal"))
    assert research_input.read_text(encoding="utf-8") == "# filled by user"
