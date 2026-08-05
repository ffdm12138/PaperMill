"""Tests for the v2 text-layer DOI evidence in extract_pdf_identity_evidence.

The decoded text layer is now the primary PDF evidence path (not a fallback
behind the raw byte scan); reference-list DOIs survive as weak evidence and
never masquerade as first-page identity.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.metadata.pdf_identity import extract_pdf_identity_evidence


DOI = "10.5194/egusphere-2026-2129"
FOREIGN = "10.9999/foreign-work"


def _fitz_pdf(path: Path, lines: list[str]) -> None:
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    for index, line in enumerate(lines):
        page.insert_text((72, 72 + 14 * index), line)
    doc.save(str(path), deflate=True)
    doc.close()


def test_text_layer_extracts_compressed_doi_as_strong(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    _fitz_pdf(pdf, ["A Study of Snow", f"https://doi.org/{DOI}", "Author One"])
    # Guard: the DOI must be invisible to the raw byte scan, otherwise this
    # test does not exercise the decoded text layer.
    assert DOI.encode() not in pdf.read_bytes()
    evidence = extract_pdf_identity_evidence(pdf_path=pdf)
    matches = [e for e in evidence.doi_evidence if e.doi == DOI]
    assert matches
    assert matches[0].source == "first_page"
    assert matches[0].labeled is True
    assert matches[0].confidence == "strong"
    assert matches[0].page_number == 1
    assert "pdf.text_layer.first_pages" in evidence.extraction_sources
    assert evidence.confidence == "explicit_identifier"


def test_unlabeled_first_page_doi_is_medium(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    _fitz_pdf(pdf, ["A Study of Snow", DOI, "Author One"])
    evidence = extract_pdf_identity_evidence(pdf_path=pdf)
    matches = [e for e in evidence.doi_evidence if e.doi == DOI]
    assert matches
    assert matches[0].source == "first_page"
    assert matches[0].labeled is False
    assert matches[0].confidence == "medium"


def test_reference_list_doi_recorded_weak(tmp_path: Path) -> None:
    # v2: reference-list DOIs are kept as weak evidence so re-audits can see
    # them, but their tier guarantees they can never drive a conflict.
    pdf = tmp_path / "paper.pdf"
    _fitz_pdf(pdf, [f"doi:{DOI}", "References", FOREIGN])
    assert FOREIGN.encode() not in pdf.read_bytes()
    evidence = extract_pdf_identity_evidence(pdf_path=pdf)
    foreign = [e for e in evidence.doi_evidence if e.doi == FOREIGN]
    assert foreign
    assert foreign[0].source == "reference_list"
    assert foreign[0].confidence == "weak"


def test_body_text_pages_are_weak(tmp_path: Path) -> None:
    fitz = pytest.importorskip("fitz")
    pdf = tmp_path / "paper.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), f"doi:{DOI}")
    page2 = doc.new_page()
    page2.insert_text((72, 72), f"https://doi.org/{FOREIGN}")
    doc.save(str(pdf), deflate=True)
    doc.close()
    evidence = extract_pdf_identity_evidence(pdf_path=pdf)
    foreign = [e for e in evidence.doi_evidence if e.doi == FOREIGN]
    assert foreign
    assert foreign[0].source == "body_text"
    assert foreign[0].confidence == "weak"
    assert foreign[0].page_number == 2
