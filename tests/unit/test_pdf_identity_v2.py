"""Extractor v2 stage tests: XMP, Document Info, Markdown, byte scan, and
the extraction_failed decision rule (all evidence paths exhausted).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.metadata.pdf_identity import (
    EXTRACTOR_VERSION,
    extract_pdf_identity_evidence,
)


DOI = "10.5194/acp-26-9643-2026"
XMP_PACKET = f"""<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
        xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/"
        xmlns:dc="http://purl.org/dc/elements/1.1/">
      <prism:doi>{DOI}</prism:doi>
      <dc:identifier>{DOI}</dc:identifier>
      <dc:subject>about 10.9999/mentioned-work</dc:subject>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""


def _fitz_pdf(path: Path, lines: list[str] | None = None) -> None:
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    for index, line in enumerate(lines or []):
        page.insert_text((72, 72 + 14 * index), line)
    doc.save(str(path), deflate=True)
    doc.close()


class TestXmpEvidence:
    def test_xmp_explicit_doi_strong(self, tmp_path: Path) -> None:
        fitz = pytest.importorskip("fitz")
        pdf = tmp_path / "paper.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Some text without a doi")
        doc.set_xml_metadata(XMP_PACKET)
        doc.save(str(pdf), deflate=True)
        doc.close()
        evidence = extract_pdf_identity_evidence(pdf_path=pdf)
        matches = [e for e in evidence.doi_evidence if e.doi == DOI]
        assert matches
        # prism:doi and dc:identifier are explicit structured keys.  (The
        # byte scan also sees the plaintext XMP packet — raw_bytes entries
        # for the same DOI are diagnostic and harmless.)
        assert any(
            e.source == "xmp_metadata" and e.confidence == "strong"
            for e in matches
        )
        assert "xmp_metadata" in evidence.extraction_sources

    def test_xmp_subject_doi_medium(self, tmp_path: Path) -> None:
        fitz = pytest.importorskip("fitz")
        pdf = tmp_path / "paper.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Some text")
        doc.set_xml_metadata(XMP_PACKET)
        doc.save(str(pdf), deflate=True)
        doc.close()
        evidence = extract_pdf_identity_evidence(pdf_path=pdf)
        mentioned = [
            e for e in evidence.doi_evidence if e.doi == "10.9999/mentioned-work"
        ]
        assert mentioned
        # dc:subject is a container field: medium at most, never strong.
        assert any(
            e.source == "xmp_metadata" and e.confidence == "medium"
            for e in mentioned
        )

    def test_malformed_xmp_does_not_kill_other_evidence(self, tmp_path: Path) -> None:
        fitz = pytest.importorskip("fitz")
        pdf = tmp_path / "paper.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), f"doi:{DOI}")
        doc.set_xml_metadata("<not-well-formed>")
        doc.save(str(pdf), deflate=True)
        doc.close()
        evidence = extract_pdf_identity_evidence(pdf_path=pdf)
        assert any(e.doi == DOI for e in evidence.doi_evidence)


class TestDocumentInfoEvidence:
    def test_docinfo_doi_key_strong(self, tmp_path: Path) -> None:
        # PyMuPDF's set_metadata rejects custom "doi" keys and its
        # metadata reader drops unknown Info entries, so the strong
        # docinfo path is unit-tested directly against the extractor
        # function with a metadata stub.
        from src.metadata.pdf_identity import _document_info_evidence

        class StubDoc:
            metadata = {
                "doi": DOI,
                "prism:doi": DOI,
                "subject": f"about 10.9999/mentioned-work",
                "keywords": "",
                "title": "A Study of Snow",
            }

        failures: list[str] = []
        evidence, _identifiers = _document_info_evidence(StubDoc(), failures)
        assert failures == []
        matches = [e for e in evidence if e.doi == DOI]
        assert matches
        assert matches[0].source == "document_info"
        assert matches[0].confidence == "strong"
        assert matches[0].labeled is True
        medium = [e for e in evidence if e.doi == "10.9999/mentioned-work"]
        assert medium
        assert medium[0].confidence == "medium"

    def test_docinfo_subject_doi_medium(self, tmp_path: Path) -> None:
        fitz = pytest.importorskip("fitz")
        pdf = tmp_path / "paper.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "body")
        doc.set_metadata({"subject": f"about {DOI}"})
        doc.save(str(pdf), deflate=True)
        doc.close()
        evidence = extract_pdf_identity_evidence(pdf_path=pdf)
        matches = [e for e in evidence.doi_evidence if e.doi == DOI]
        assert matches
        assert matches[0].confidence == "medium"


class TestMarkdownEvidence:
    def test_markdown_front_matter_medium_and_reference_weak(
        self, tmp_path: Path
    ) -> None:
        pdf = tmp_path / "paper.pdf"
        _fitz_pdf(pdf, ["doi:10.9999/textlayer26"])
        markdown = tmp_path / "paper.md"
        markdown.write_text(
            f"# A Study of Snow\n\nAuthors: Smith, Jones\n\nhttps://doi.org/{DOI}\n\n"
            "## References\n\n10.1234/refone26\n",
            encoding="utf-8",
        )
        evidence = extract_pdf_identity_evidence(
            pdf_path=pdf, markdown_path=markdown
        )
        front = [e for e in evidence.doi_evidence if e.doi == DOI]
        assert front
        assert front[0].source == "front_matter"
        assert front[0].confidence == "medium"
        reference = [e for e in evidence.doi_evidence if e.doi == "10.1234/refone26"]
        assert reference
        assert reference[0].source == "reference_list"
        assert reference[0].confidence == "weak"
        assert evidence.canonical_title == "a study of snow"
        assert "Smith" in evidence.author_families


class TestByteScanDiagnostic:
    def test_byte_scan_is_weak_diagnostic(self, tmp_path: Path) -> None:
        # Not a real PDF: fitz fails, only the bounded byte scan can see the
        # plaintext DOI.  It is weak evidence, never decisive.
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.7\nDOI 10.1234/example123\n")
        evidence = extract_pdf_identity_evidence(pdf_path=pdf)
        matches = [e for e in evidence.doi_evidence if e.doi == "10.1234/example123"]
        assert matches
        # Whatever the PDF parser does with this fake file, the only DOI
        # source is the byte scan, and it is weak evidence.
        assert all(e.source == "raw_bytes" for e in matches)
        assert all(e.confidence == "weak" for e in matches)
        assert "pdf.raw_bytes.diagnostic" in evidence.extraction_sources

    def test_byte_scan_rejected_fragments_warn(self, tmp_path: Path) -> None:
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(
            b"%PDF-1.7\n10.1103/P and 10.1073/pnas. and 10.1073/pnas.xxxxxxxxxx\n"
        )
        evidence = extract_pdf_identity_evidence(pdf_path=pdf)
        assert any("rejected" in w for w in evidence.warnings)
        assert not any(
            e.doi in {"10.1103/P", "10.1073/pnas", "10.1073/pnas.xxxxxxxxxx"}
            for e in evidence.doi_evidence
        )


class TestExtractionFailedDecision:
    def test_corrupt_pdf_no_evidence_is_unreadable(self, tmp_path: Path) -> None:
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.7\nnot a real pdf, no doi here\n")
        evidence = extract_pdf_identity_evidence(pdf_path=pdf)
        assert evidence.confidence == "unreadable"
        assert evidence.parser_failures
        assert evidence.doi_evidence == ()
        assert evidence.extracted_identifiers == ()

    def test_corrupt_pdf_with_markdown_rescue(self, tmp_path: Path) -> None:
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.7\nnot a real pdf\n")
        markdown = tmp_path / "paper.md"
        markdown.write_text(
            f"# A Study of Snow\n\nAuthors: Smith\n\nhttps://doi.org/{DOI}\n",
            encoding="utf-8",
        )
        evidence = extract_pdf_identity_evidence(
            pdf_path=pdf, markdown_path=markdown
        )
        # Markdown rescues the identity: NOT unreadable.
        assert evidence.confidence != "unreadable"
        assert any(e.doi == DOI for e in evidence.doi_evidence)
        assert evidence.canonical_title == "a study of snow"

    def test_corrupt_pdf_with_partial_fields_is_not_unreadable(
        self, tmp_path: Path
    ) -> None:
        # A hard parse error with usable structured evidence must not be
        # classified as extraction_failed (F rule: all paths exhausted).
        fitz = pytest.importorskip("fitz")
        pdf = tmp_path / "paper.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), f"doi:{DOI}")
        doc.set_xml_metadata("<broken xml")
        doc.save(str(pdf), deflate=True)
        doc.close()
        evidence = extract_pdf_identity_evidence(pdf_path=pdf)
        assert any(e.doi == DOI for e in evidence.doi_evidence)
        assert evidence.confidence == "explicit_identifier"


class TestDeterminism:
    def test_extract_twice_identical(self, tmp_path: Path) -> None:
        fitz = pytest.importorskip("fitz")
        pdf = tmp_path / "paper.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), f"doi:{DOI}")
        page.insert_text((72, 100), "References")
        page.insert_text((72, 114), "10.1234/refone26")
        doc.set_xml_metadata(XMP_PACKET)
        doc.save(str(pdf), deflate=True)
        doc.close()
        first = extract_pdf_identity_evidence(pdf_path=pdf)
        second = extract_pdf_identity_evidence(pdf_path=pdf)
        assert first.to_dict() == second.to_dict()

    def test_evidence_sorted_and_deduped(self, tmp_path: Path) -> None:
        fitz = pytest.importorskip("fitz")
        pdf = tmp_path / "paper.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), f"doi:{DOI}")
        page.insert_text((72, 86), f"doi:{DOI}")
        doc.save(str(pdf), deflate=True)
        doc.close()
        evidence = extract_pdf_identity_evidence(pdf_path=pdf)
        keys = [
            (e.doi, e.source, e.page_number, e.labeled)
            for e in evidence.doi_evidence
        ]
        assert len(keys) == len(set(keys))


class TestAuthorLineExtraction:
    def test_section_headers_never_become_author_families(self, tmp_path: Path) -> None:
        # Single-word section headers and header-like lines must never be
        # extracted as author families (real-corpus pollution found in the
        # 2026-07-31 audit: "ARTICLE"/"Abstract" were becoming families).
        pdf = tmp_path / "paper.pdf"
        _fitz_pdf(pdf, [
            "A Study of Snow",
            "ARTICLE",
            "Abstract",
            "doi:10.5194/acp-26-9643-2026",
            "Received: 12 January 2024",
            "Accepted: 3 March 2024",
        ])
        evidence = extract_pdf_identity_evidence(pdf_path=pdf)
        assert evidence.author_families == ()

    def test_author_line_extracts_families(self, tmp_path: Path) -> None:
        pdf = tmp_path / "paper.pdf"
        _fitz_pdf(pdf, [
            "A Study of Snow",
            "Jane Smith, John Jones",
            "doi:10.5194/acp-26-9643-2026",
        ])
        evidence = extract_pdf_identity_evidence(pdf_path=pdf)
        assert "Smith" in evidence.author_families
        assert "Jones" in evidence.author_families


class TestVersionField:
    def test_extractor_version_recorded(self, tmp_path: Path) -> None:
        pdf = tmp_path / "paper.pdf"
        _fitz_pdf(pdf, [f"doi:{DOI}"])
        evidence = extract_pdf_identity_evidence(pdf_path=pdf)
        assert evidence.identity_extractor_version == EXTRACTOR_VERSION
        assert evidence.to_dict()["identity_extractor_version"] == EXTRACTOR_VERSION
