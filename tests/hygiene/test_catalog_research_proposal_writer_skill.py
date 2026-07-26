"""Hygiene contract for the catalog_research_proposal_writer skill."""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

pytestmark = pytest.mark.hygiene

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILL_DIR = _REPO_ROOT / "skills" / "catalog_research_proposal_writer"


def test_proposal_writer_skill_files_exist():
    for name in [
        "SKILL.md",
        "README.md",
        "CLAUDE.md",
        "proposal_plan_schema.json",
        "examples/example_proposal_plan.json",
        "examples/example_research_input.md",
        "examples/example_methods_design.md",
        "examples/example_proposal_main.tex",
    ]:
        assert (_SKILL_DIR / name).exists(), f"missing skill file: {name}"


def test_proposal_writer_skill_has_frontmatter():
    text = (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "name: catalog_research_proposal_writer" in text
    assert "description:" in text


def test_proposal_writer_skill_documents_boundaries():
    text = (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "selected_catalog.json" in text
    assert "input/research_input.md" in text
    assert "（待填）" in text
    assert "Do not read `data/papers` directly" in text
    assert "Do not read `data/paper_raw`" in text
    assert "article/<paper_number>" in text
    assert "write/jobs/<job_id>/tex/" in text
    assert "write/jobs/<job_id>/planning/" in text
    assert "Do not guess DOI" in text
    assert "bibtex_for_entry" in text
    assert "export_write_job_bib.py" in text
    assert "read_decision" in text and "pending" in text and "不得" in text
    lowered = text.lower()
    assert "rag" in lowered and "embedding" in lowered


def test_proposal_writer_skill_documents_plan_honesty():
    text = (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "先射箭后画靶：先综述与问题，再写方法，再规划结果与数据分析。" in text
    assert "a research plan, not results" in text
    assert "研究计划" in text
    assert "不得虚构数据" in text
    assert "grounded_in" in text
    assert "results_plan" in text
    assert "planned" in text
    assert "Quality Acceptance" in text
    assert "X指出：X" in text
    assert "check_write_planning_docs.py" in text
    assert "check_write_quality_text.py" in text


def test_proposal_plan_example_conforms_to_schema():
    schema = json.loads(
        (_SKILL_DIR / "proposal_plan_schema.json").read_text(encoding="utf-8")
    )
    example = json.loads(
        (_SKILL_DIR / "examples" / "example_proposal_plan.json").read_text(encoding="utf-8")
    )
    jsonschema.validate(instance=example, schema=schema)


def test_proposal_plan_schema_enforces_grounding():
    schema = json.loads(
        (_SKILL_DIR / "proposal_plan_schema.json").read_text(encoding="utf-8")
    )
    assert schema["properties"]["workflow"] == {"const": "catalog_research_proposal"}
    method_items = schema["properties"]["methods_design"]["items"]
    assert "grounded_in" in method_items["required"]
    assert method_items["properties"]["grounded_in"]["minItems"] == 1
    result_items = schema["properties"]["results_plan"]["items"]
    assert result_items["properties"]["status"] == {"const": "planned"}


def test_catalog_tex_writer_boundary_names_proposal_writer():
    text = (_REPO_ROOT / "skills" / "catalog_tex_writer" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "catalog_research_proposal_writer" in text
