"""Integration tests for scripts/check_write_planning_docs.py (synthetic jobs)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from scripts.check_write_planning_docs import check_planning_docs
from scripts.export_write_job_bib import export_job_references
from src.writer.bib import bib_key_for_entry

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


def _substantial(text: str) -> str:
    return text + "\n\n" + "该部分内容基于入选论文的 catalog 证据整理，覆盖研究对象、方法与数据、关键结论与局限。\n"


def _make_review_job(tmp_path: Path, job_id: str = "001_review_demo_abc123"):
    job_dir = tmp_path / "jobs" / job_id
    papers = []
    for n in range(1, 4):
        number = f"{n:016d}"
        paper_name = f"200{n}_Author{n}_synthetic"
        metadata = _metadata(n, f"Author{n}", 2000 + n)
        folder = job_dir / "article" / number
        folder.mkdir(parents=True)
        (folder / f"{paper_name}.metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        bib_key = bib_key_for_entry({"paper_name": paper_name, "metadata": metadata})
        papers.append({"paper_number": number, "paper_name": paper_name,
                       "bib_key": bib_key})
    (job_dir / "selected_catalog.json").write_text(json.dumps({
        "schema_version": "1.0", "job_id": job_id, "source_categories": ["测试"],
        "papers": [{"paper_number": p["paper_number"], "paper_name": p["paper_name"]}
                   for p in papers],
    }, ensure_ascii=False), encoding="utf-8")
    (job_dir / "job.json").write_text(json.dumps({
        "schema_version": "1.0", "job_id": job_id, "workflow": "catalog_review",
    }), encoding="utf-8")

    planning = job_dir / "planning"
    reports = job_dir / "reports"
    planning.mkdir()
    reports.mkdir()
    matrix_rows = ["| paper_number | bib_key | 研究对象 | 方法与数据 | 关键结论 | 综述角色 | 局限 | 定量结论 |",
                   "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for p in papers:
        matrix_rows.append(
            f"| {p['paper_number']} | {p['bib_key']} | 对象 | 方法 | 结论 | 角色 | 局限 | 数值 |")
    (reports / "literature_matrix.md").write_text(
        _substantial("\n".join(matrix_rows)), encoding="utf-8")
    (planning / "review_outline.md").write_text(
        _substantial("# 综述大纲\n\n围绕机制链组织。"), encoding="utf-8")
    (planning / "research_gaps.md").write_text(
        _substantial("# 研究空白\n\nG01：验证缺失。"), encoding="utf-8")
    (planning / "proposed_directions.md").write_text(
        _substantial("# 潜在方向\n\nD01：建立基准。"), encoding="utf-8")

    plan = {
        "schema_version": "1.0",
        "job_id": job_id,
        "workflow": "catalog_review",
        "title_zh": "合成主题综述",
        "topic_zh": "合成主题",
        "language": "zh",
        "source_categories": ["测试"],
        "paper_pool": [
            {"paper_number": p["paper_number"], "paper_name": p["paper_name"],
             "bib_key": p["bib_key"], "role_in_review_zh": "证据"}
            for p in papers
        ],
        "themes": [
            {"theme_id": "T01", "theme_zh": "主题一", "thesis_zh": "论点一",
             "evidence": [
                 {"paper_number": papers[0]["paper_number"],
                  "bib_key": papers[0]["bib_key"], "basis_zh": "依据一"},
                 {"paper_number": papers[1]["paper_number"],
                  "bib_key": papers[1]["bib_key"], "basis_zh": "依据二"},
             ]},
            {"theme_id": "T02", "theme_zh": "主题二", "thesis_zh": "论点二",
             "evidence": [
                 {"paper_number": papers[1]["paper_number"],
                  "bib_key": papers[1]["bib_key"], "basis_zh": "依据三"},
                 {"paper_number": papers[2]["paper_number"],
                  "bib_key": papers[2]["bib_key"], "basis_zh": "依据四"},
             ]},
        ],
        "research_gaps": [
            {"gap_id": "G01", "gap_zh": "空白一", "why_it_matters_zh": "重要性",
             "evidence": [{"paper_number": papers[0]["paper_number"],
                           "bib_key": papers[0]["bib_key"], "basis_zh": "依据"}]},
        ],
        "proposed_directions": [
            {"direction_id": "D01", "direction_zh": "方向一", "rationale_zh": "理由",
             "addresses_gap_ids": ["G01"],
             "builds_on": [{"paper_number": papers[2]["paper_number"],
                            "bib_key": papers[2]["bib_key"], "basis_zh": "基础"}],
             "feasibility_zh": "可行"},
        ],
        "sections": [
            {"file": "sections/introduction.tex", "title_zh": "引言", "purpose_zh": "背景"},
            {"file": "sections/theme_one.tex", "title_zh": "主题一", "purpose_zh": "主题",
             "theme_ids": ["T01"]},
            {"file": "sections/research_gaps.tex", "title_zh": "空白", "purpose_zh": "空白"},
            {"file": "sections/conclusion.tex", "title_zh": "结论", "purpose_zh": "收束"},
        ],
    }
    (planning / "review_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    export = export_job_references(argparse.Namespace(
        job_id=job_id, write_dir=tmp_path / "jobs", paper_numbers=None))
    assert export["passed"] is True
    return job_dir, papers, plan


def _args(tmp_path: Path, job_id: str, profile: str | None = None):
    return argparse.Namespace(job_id=job_id, write_dir=tmp_path / "jobs",
                              profile=profile)


def test_valid_review_job_passes(tmp_path: Path):
    job_dir, _, _ = _make_review_job(tmp_path)
    result = check_planning_docs(_args(tmp_path, job_dir.name))
    assert result["errors"] == []
    assert result["passed"] is True
    report = json.loads(
        (job_dir / "reports" / "planning_docs_check_report.json").read_text(encoding="utf-8"))
    assert report["passed"] is True


def test_placeholder_in_intermediate_fails(tmp_path: Path):
    job_dir, _, _ = _make_review_job(tmp_path)
    outline = job_dir / "planning" / "review_outline.md"
    outline.write_text(outline.read_text(encoding="utf-8") + "\n（待填）\n",
                       encoding="utf-8")
    result = check_planning_docs(_args(tmp_path, job_dir.name))
    assert result["passed"] is False
    assert any("review_outline.md" in err for err in result["errors"])


def test_unknown_bib_key_fails(tmp_path: Path):
    job_dir, _, plan = _make_review_job(tmp_path)
    plan["research_gaps"][0]["evidence"][0]["bib_key"] = "ghost2099nowhere"
    (job_dir / "planning" / "review_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    result = check_planning_docs(_args(tmp_path, job_dir.name))
    assert result["passed"] is False
    assert any("ghost2099nowhere" in err for err in result["errors"])


def test_paper_pool_mismatch_fails(tmp_path: Path):
    job_dir, _, plan = _make_review_job(tmp_path)
    plan["paper_pool"] = plan["paper_pool"][:2] + [dict(
        plan["paper_pool"][2], paper_number="0000000000000099")]
    (job_dir / "planning" / "review_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    result = check_planning_docs(_args(tmp_path, job_dir.name))
    assert result["passed"] is False
    assert any("paper_pool mismatch" in err for err in result["errors"])


def test_empty_selected_catalog_fails_closed(tmp_path: Path):
    """An empty paper pool must never skip the deep checks (fail-open regression)."""
    job_dir, _, _ = _make_review_job(tmp_path)
    selected = json.loads(
        (job_dir / "selected_catalog.json").read_text(encoding="utf-8"))
    selected["papers"] = []
    (job_dir / "selected_catalog.json").write_text(
        json.dumps(selected, ensure_ascii=False), encoding="utf-8")
    (job_dir / "tex" / "references.bib").unlink()

    result = check_planning_docs(_args(tmp_path, job_dir.name))
    assert result["passed"] is False
    assert any("selects no papers" in err for err in result["errors"])
    assert any("references.bib" in err for err in result["errors"])


def test_missing_job_json_still_writes_report(tmp_path: Path):
    job_dir, _, _ = _make_review_job(tmp_path)
    (job_dir / "job.json").unlink()
    result = check_planning_docs(_args(tmp_path, job_dir.name))
    assert result["passed"] is False
    report = json.loads(
        (job_dir / "reports" / "planning_docs_check_report.json").read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["job_id"] == job_dir.name


def test_malformed_plan_json_reports_error_not_traceback(tmp_path: Path):
    job_dir, _, _ = _make_review_job(tmp_path)
    (job_dir / "planning" / "review_plan.json").write_text("{not json", encoding="utf-8")
    result = check_planning_docs(_args(tmp_path, job_dir.name))
    assert result["passed"] is False
    assert any("invalid JSON" in err for err in result["errors"])


@pytest.mark.parametrize("placeholder", ["TODO", "待填", "TEMPLATE_ONLY", "由大模型补全"])
def test_proposal_research_input_rejects_every_todo_marker(tmp_path: Path, placeholder: str):
    job_dir, _, _ = _make_review_job(tmp_path, job_id="003_proposal_markers_ghi789")
    meta = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    meta["workflow"] = "catalog_research_proposal"
    (job_dir / "job.json").write_text(json.dumps(meta), encoding="utf-8")
    (job_dir / "input").mkdir()
    (job_dir / "input" / "research_input.md").write_text(
        _substantial(f"# 研究项目描述\n\n## 研究问题\n\n{placeholder}\n"), encoding="utf-8")

    result = check_planning_docs(_args(tmp_path, job_dir.name))
    assert result["passed"] is False
    assert any("research_input.md" in err and "placeholder" in err
               for err in result["errors"])


def test_proposal_research_input_rejects_near_empty_file(tmp_path: Path):
    job_dir, _, _ = _make_review_job(tmp_path, job_id="004_proposal_short_jkl012")
    meta = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    meta["workflow"] = "catalog_research_proposal"
    (job_dir / "job.json").write_text(json.dumps(meta), encoding="utf-8")
    (job_dir / "input").mkdir()
    (job_dir / "input" / "research_input.md").write_text("# 研究项目描述\n", encoding="utf-8")

    result = check_planning_docs(_args(tmp_path, job_dir.name))
    assert result["passed"] is False
    assert any("research_input.md" in err and "empty" in err for err in result["errors"])


def test_proposal_requires_filled_research_input(tmp_path: Path):
    job_dir, papers, plan = _make_review_job(tmp_path, job_id="002_proposal_demo_def456")
    meta = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    meta["workflow"] = "catalog_research_proposal"
    (job_dir / "job.json").write_text(json.dumps(meta), encoding="utf-8")
    (job_dir / "input").mkdir()
    (job_dir / "input" / "research_input.md").write_text(
        "# 研究项目描述\n\n## 研究问题\n\n（待填）\n", encoding="utf-8")
    result = check_planning_docs(_args(tmp_path, job_dir.name))
    assert result["passed"] is False
    assert any("research_input.md" in err for err in result["errors"])
