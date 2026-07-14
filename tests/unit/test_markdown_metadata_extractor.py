"""Tests for markdown_metadata_extractor — extraction from MinerU markdown content."""
import pytest
from pathlib import Path

from src.services.markdown_metadata_extractor import (
    ExtractedMetadata,
    _extract_dois,
    _extract_title_candidates,
    _extract_author_candidates,
    _extract_year_candidates,
    _extract_abstract,
    extract_front_matter_candidates_from_markdown,
    extract_metadata_from_markdown,
)


# ── DOI extraction ─────────────────────────────────────────────────────

def test_extract_dois_standard():
    text = "See https://doi.org/10.1234/abcd.5678 for details"
    dois = _extract_dois(text)
    assert "10.1234/abcd.5678" in dois


def test_extract_dois_multiple():
    text = "DOI: 10.1000/a and also 10.2000/b"
    dois = _extract_dois(text)
    assert "10.1000/a" in dois
    assert "10.2000/b" in dois


def test_extract_dois_empty():
    assert _extract_dois("no doi here at all") == []


def test_extract_dois_trailing_punctuation():
    text = "see 10.1234/abcd.5678."
    dois = _extract_dois(text)
    assert "10.1234/abcd.5678" in dois


def test_extract_doi_with_garbled_mineru_output():
    text = "DOI 10.1007/s11433-008-0106-6 garbled 10. 1007/ s11433"
    dois = _extract_dois(text)
    # at least the clean one is found
    assert any("s11433" in d for d in dois)


# ── Title extraction ───────────────────────────────────────────────────

def test_extract_title_from_heading():
    lines = ["# Saltation and suspension of wind-blown particle movement", "", "body"]
    titles = _extract_title_candidates(lines)
    assert titles and "saltation" in titles[0].lower()


def test_extract_title_no_heading():
    lines = ["", "A very long title line without any heading marker", "more"]
    titles = _extract_title_candidates(lines)
    assert titles and "long title line" in titles[0].lower()


def test_extract_title_skips_abstract():
    lines = ["# Abstract", "this is some longer body content line"]
    titles = _extract_title_candidates(lines)
    # "# Abstract" heading is too short / a non-title pattern, so it is not taken as the title
    assert all("abstract" not in t.lower() for t in titles)


# ── Author extraction ──────────────────────────────────────────────────

def test_extract_authors_simple():
    lines = ["# Some Title", "WANG Ping, ZHENG XiaoJing, HU WenWen", ""]
    authors = _extract_author_candidates(lines)
    assert authors
    flat = [a for sub in authors for a in sub]
    assert any("WANG" in a or "Ping" in a for a in flat)


def test_extract_authors_with_and():
    lines = ["# Some Title", "Alice Smith and Bob Jones", ""]
    authors = _extract_author_candidates(lines)
    assert authors
    flat = [a for sub in authors for a in sub]
    assert any("Alice" in a or "Smith" in a for a in flat)


def test_extract_authors_skips_affiliations():
    lines = ["# Some Title", "University of Somewhere, Department of Physics", ""]
    authors = _extract_author_candidates(lines)
    # heavy affiliation markers should be skipped
    assert authors == [] or not any("University" in a for sub in authors for a in sub)


# ── Year extraction ────────────────────────────────────────────────────

def test_extract_year_copyright():
    text = "© 2019 Some Publisher"
    lines = text.split("\n")
    years = _extract_year_candidates(text, lines)
    assert 2019 in years


def test_extract_year_received():
    text = "Received 15 March 2020"
    lines = text.split("\n")
    years = _extract_year_candidates(text, lines)
    assert 2020 in years


def test_extract_year_none():
    text = "no year anywhere"
    lines = text.split("\n")
    assert _extract_year_candidates(text, lines) == []


# ── Abstract extraction ────────────────────────────────────────────────

def test_extract_abstract():
    text = (
        "# Title\n## Abstract\n"
        "Several factors that affect the trajectories of sand particles are "
        "analyzed in this study with detailed experiments and modelling.\n"
        "## Introduction\nbody"
    )
    abstract = _extract_abstract(text)
    assert "trajectories" in abstract.lower()


def test_extract_abstract_missing():
    text = "# Title\nAuthors\n## Introduction\nContent."
    abstract = _extract_abstract(text)
    assert abstract == ""


# ── Full extract from file ─────────────────────────────────────────────

def test_extract_from_markdown_file(tmp_path):
    md = tmp_path / "doc.md"
    md.write_text("""# Saltation and suspension of wind-blown particle movement
WANG Ping, ZHENG XiaoJing, HU WenWen
Key Laboratory of Mechanics, Lanzhou University, Lanzhou 730000, China

## Abstract
Several factors that affect the trajectories of sand particles are analyzed.
Keywords: wind erosion, saltation, suspension
DOI: 10.1007/s11433-008-0106-6

## Introduction
Wind-blown sand movement is a complex process.
""", encoding="utf-8")

    result = extract_metadata_from_markdown(md, paper_name="test")
    assert result.doi_candidates
    assert any("s11433" in d for d in result.doi_candidates)
    assert result.title_candidates
    assert "saltation" in result.title_candidates[0].lower()


def test_extract_from_missing_file():
    result = extract_metadata_from_markdown("/nonexistent/doc.md")
    assert "not found" in result.warnings[0].lower()


def test_front_matter_title_in_first_line(tmp_path):
    md = tmp_path / "doc.md"
    md.write_text("# Correct Title About Snow Transport\nAlice Smith, Bob Jones\n", encoding="utf-8")
    result = extract_front_matter_candidates_from_markdown(md)
    assert result.title_candidates == ["Correct Title About Snow Transport"]
    assert result.author_candidates


def test_front_matter_title_on_line_50_is_used(tmp_path):
    md = tmp_path / "doc.md"
    md.write_text("\n".join([""] * 49 + ["# Correct Title About Snow Transport"]), encoding="utf-8")
    result = extract_front_matter_candidates_from_markdown(md)
    assert result.title_candidates == ["Correct Title About Snow Transport"]


def test_front_matter_title_on_line_101_is_ignored(tmp_path):
    md = tmp_path / "doc.md"
    md.write_text("\n".join([""] * 100 + ["# Correct Title About Snow Transport"]), encoding="utf-8")
    result = extract_front_matter_candidates_from_markdown(md)
    assert result.title_candidates == []


def test_front_matter_skips_non_title_lines(tmp_path):
    md = tmp_path / "doc.md"
    md.write_text(
        "\n".join([
            "DOI: 10.1000/example",
            "alice@example.org",
            "Department of Physics, University of Somewhere",
            "Abstract",
            "Keywords: snow, transport",
        ]),
        encoding="utf-8",
    )
    result = extract_front_matter_candidates_from_markdown(md)
    assert result.title_candidates == []


def test_front_matter_merges_adjacent_title_lines_within_first_100(tmp_path):
    md = tmp_path / "doc.md"
    md.write_text(
        "# Simulation of wind-induced snow transport\n"
        "over complex alpine terrain\n"
        "Vionnet, V.\n",
        encoding="utf-8",
    )
    result = extract_front_matter_candidates_from_markdown(md)
    assert result.title_candidates
    assert "complex alpine terrain" in result.title_candidates[0]


def test_front_matter_title_merge_does_not_cross_line_100(tmp_path):
    md = tmp_path / "doc.md"
    md.write_text(
        "\n".join([""] * 99 + [
            "# Boundary Title Within Window",
            "continuation outside window",
        ]),
        encoding="utf-8",
    )
    result = extract_front_matter_candidates_from_markdown(md)
    assert result.title_candidates == ["Boundary Title Within Window"]


# ── Edge cases ─────────────────────────────────────────────────────────

def test_empty_file(tmp_path):
    md = tmp_path / "empty.md"
    md.write_text("", encoding="utf-8")
    result = extract_metadata_from_markdown(md)
    assert result.doi_candidates == []
    assert result.title_candidates == []
