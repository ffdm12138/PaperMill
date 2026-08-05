from __future__ import annotations
import json
from pathlib import Path
import pytest

from src.utils.file_fingerprint import compute_sha256
from src.metadata.freeze import assert_metadata_frozen, assert_metadata_write_allowed
from src.metadata.pdf_identity import DoiEvidence, PdfIdentityEvidence
from src.metadata.pdf_match import build_match_receipt, validate_metadata_match_receipt
from tests.integration.test_frozen_v32_transaction_pipeline import NUMBER, _workspace


def _metadata() -> dict:
    return {
        "title": {"original": "A"},
        "authors": [{"family": "Smith"}],
        "first_author": {"family": "Smith"},
        "year": 2024,
        "identifiers": {"doi": "10.1234/a"},
    }


def test_requested_doi_without_pdf_identity_is_unverifiable(tmp_path: Path):
    # A requested DOI with no usable PDF identity evidence is unverifiable
    # under the v2 policy — never the old mismatch.
    folder = tmp_path
    metadata = _metadata()
    meta = folder / f"{NUMBER}.metadata.json"
    pdf = folder / f"{NUMBER}.pdf"
    meta.write_text(json.dumps(metadata), encoding="utf-8")
    pdf.write_bytes(b"%PDF no identifier")
    evidence = PdfIdentityEvidence(
        pdf_sha256="",
        doi_evidence=(),
        canonical_title=None,
        publication_year=None,
        first_author_family=None,
        author_families=(),
        extracted_identifiers=(),
        extraction_sources=("test",),
        confidence="missing",
        parser_failures=(),
        warnings=(),
    )
    receipt = build_match_receipt(
        folder, NUMBER, metadata, evidence, requested_doi="10.1234/a"
    )
    assert receipt["match_method"] == "unverifiable"
    assert receipt["match_status"] == "unverifiable"
    assert receipt["schema_version"] == "2.0"


def test_identifier_conflict_cannot_be_manually_confirmed(tmp_path: Path):
    folder = tmp_path
    metadata = _metadata()
    meta = folder / f"{NUMBER}.metadata.json"
    pdf = folder / f"{NUMBER}.pdf"
    meta.write_text(json.dumps(metadata), encoding="utf-8")
    pdf.write_bytes(b"%PDF")
    # A unique structured foreign primary DOI plus contradictory
    # bibliographic fields -> the only automatic conflict path.
    evidence = PdfIdentityEvidence(
        pdf_sha256="",
        doi_evidence=(
            DoiEvidence(
                doi="10.1234/b",
                source="xmp_metadata",
                page_number=None,
                labeled=True,
                context="xmp doi",
                confidence="strong",
            ),
        ),
        canonical_title="A Completely Different Title",
        publication_year=2024,
        first_author_family="Zhang",
        author_families=("Zhang",),
        extracted_identifiers=(),
        extraction_sources=("test",),
        confidence="explicit_identifier",
        parser_failures=(),
        warnings=(),
    )
    manual = {
        "confirmed_by": "admin",
        "reason": "looked",
        "confirmed_at": "2026-01-01T00:00:00+00:00",
        "metadata_sha256": compute_sha256(meta),
        "pdf_sha256": compute_sha256(pdf),
    }
    receipt = build_match_receipt(folder, NUMBER, metadata, evidence, manual=manual)
    assert receipt["match_method"] == "identifier_conflict"
    assert receipt["match_status"] == "identifier_conflict"
    assert receipt["manual_errors"] == [
        "manual confirmation cannot override identifier conflict"
    ]


@pytest.mark.parametrize("asset", ["pdf", "metadata_match", "source_record"])
def test_freeze_guard_replays_entire_evidence_closure(tmp_path: Path, asset: str):
    workspace, _, _, _ = _workspace(tmp_path)
    if asset == "pdf":
        workspace.pdf.write_bytes(workspace.pdf.read_bytes() + b"tamper")
    elif asset == "metadata_match":
        value = json.loads(workspace.metadata_match.read_text(encoding="utf-8"))
        value["match_status"] = "unverifiable"
        workspace.metadata_match.write_text(json.dumps(value), encoding="utf-8")
    else:
        record = next(workspace.source_records.glob("*.json"))
        record.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        assert_metadata_frozen(workspace.root, NUMBER)


def test_frozen_metadata_rejects_normal_writer(tmp_path: Path):
    workspace, _, _, _ = _workspace(tmp_path)
    with pytest.raises(PermissionError):
        assert_metadata_write_allowed(workspace.root, NUMBER)
