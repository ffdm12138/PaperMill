"""Hygiene contract for the catalog_review_writer skill."""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

pytestmark = pytest.mark.hygiene

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILL_DIR = _REPO_ROOT / "skills" / "catalog_review_writer"


def test_review_writer_skill_files_exist():
    for name in [
        "SKILL.md",
        "README.md",
        "CLAUDE.md",
        "review_plan_schema.json",
        "examples/example_review_plan.json",
        "examples/example_literature_matrix.md",
        "examples/example_research_gaps.md",
        "examples/example_review_main.tex",
    ]:
        assert (_SKILL_DIR / name).exists(), f"missing skill file: {name}"


def test_review_writer_skill_has_frontmatter():
    text = (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "name: catalog_review_writer" in text
    assert "description:" in text


def test_review_writer_skill_documents_boundaries():
    text = (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "selected_catalog.json" in text
    assert "Do not read `data/papers` directly" in text
    assert "Do not read `data/paper_raw`" in text
    assert "article/<paper_number>" in text
    assert "write/jobs/<job_id>/tex/" in text
    assert "write/jobs/<job_id>/planning/" in text
    assert "Do not guess DOI" in text
    assert "bibtex_from_metadata" in text
    assert "bibtex_for_entry" in text
    assert "export_write_job_bib.py" in text
    assert "read_decision" in text and "pending" in text and "不得" in text
    lowered = text.lower()
    assert "rag" in lowered and "embedding" in lowered


def test_review_writer_skill_documents_quality_acceptance():
    text = (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "Quality Acceptance" in text
    assert "X指出：X" in text
    assert "study object" in text
    assert "method/data" in text
    assert "problem chain or mechanism chain" in text
    assert "Quantitative claims" in text
    assert "literature_matrix.md" in text
    assert "paper_number + bib_key" in text
    assert "write/jobs/*" in text
    assert "check_write_planning_docs.py" in text
    assert "check_write_quality_text.py" in text


def test_review_plan_example_conforms_to_schema():
    schema = json.loads((_SKILL_DIR / "review_plan_schema.json").read_text(encoding="utf-8"))
    example = json.loads(
        (_SKILL_DIR / "examples" / "example_review_plan.json").read_text(encoding="utf-8")
    )
    jsonschema.validate(instance=example, schema=schema)


def test_review_plan_schema_requires_evidence():
    schema = json.loads((_SKILL_DIR / "review_plan_schema.json").read_text(encoding="utf-8"))
    assert schema["properties"]["workflow"] == {"const": "catalog_review"}
    gap_items = schema["properties"]["research_gaps"]["items"]
    assert "evidence" in gap_items["required"]
    evidence_ref = schema["definitions"]["evidenceRef"]
    assert "paper_number" in evidence_ref["required"]
    assert "bib_key" in evidence_ref["required"]


def test_catalog_tex_writer_boundary_names_review_writer():
    text = (_REPO_ROOT / "skills" / "catalog_tex_writer" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "catalog_review_writer" in text
    assert "the only default article-writing skill" not in text
