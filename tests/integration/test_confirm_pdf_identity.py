"""Manual confirmation protocol (automatic/final receipt model).

Overridable: ambiguous / unverifiable / related_version -> manual_confirmed.
identifier_conflict is a hard, non-overridable conclusion.  The
--expected-receipt-sha256 window closes when the receipt changed.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from src.metadata.pdf_identity import DoiEvidence, PdfIdentityEvidence
from src.metadata.pdf_match import build_match_receipt, write_match_receipt
from src.utils.file_fingerprint import compute_sha256

REPO_ROOT = Path(__file__).resolve().parents[2]
NUMBER = "0000000000000001"


def _load_script() -> object:
    spec = importlib.util.spec_from_file_location(
        "confirm_paper_raw_pdf_identity",
        REPO_ROOT / "scripts" / "confirm_paper_raw_pdf_identity.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ambiguous_workspace(root: Path) -> Path:
    """A workspace whose automatic decision is ambiguous (medium foreign)."""
    folder = root / NUMBER
    folder.mkdir(parents=True)
    metadata = {
        "title": {"original": "A Study of Snow"},
        "authors": [{"family": "Smith"}, {"family": "Jones"}],
        "first_author": {"family": "Smith"},
        "year": 2024,
        "identifiers": {"doi": "10.5194/egusphere-2025-5135"},
    }
    (folder / f"{NUMBER}.metadata.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )
    (folder / f"{NUMBER}.pdf").write_bytes(b"%PDF minimal")
    (folder / f"{NUMBER}.paper.number").write_text(
        json.dumps({"paper_number": NUMBER, "folder_name": NUMBER, "state": "active"}),
        encoding="utf-8",
    )
    evidence = PdfIdentityEvidence(
        pdf_sha256="",
        doi_evidence=(
            DoiEvidence(
                doi="10.1007/s10546-021-00629",
                source="document_info",
                page_number=None,
                labeled=False,
                context="subject",
                confidence="medium",
            ),
        ),
        canonical_title="A Study of Snow",
        publication_year=2024,
        first_author_family="Smith",
        author_families=("Smith", "Jones"),
        extracted_identifiers=(),
        extraction_sources=("test",),
        confidence="explicit_identifier",
        parser_failures=(),
        warnings=(),
    )
    receipt = build_match_receipt(folder, NUMBER, metadata, evidence)
    assert receipt["match_status"] == "ambiguous"
    write_match_receipt(folder, receipt)
    return folder


def _run(module, root: Path, *argv: str, capsys) -> int:
    saved = sys.argv
    sys.argv = ["confirm_paper_raw_pdf_identity.py",
                "--paper-number", NUMBER, "--paper-raw-dir", str(root), *argv]
    try:
        return module.main()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = saved


class TestConfirmProtocol:
    def test_ambiguous_confirmed_to_matched(self, tmp_path: Path, capsys) -> None:
        folder = _ambiguous_workspace(tmp_path)
        receipt_path = folder / f"{NUMBER}.metadata_match.json"
        expected_sha = compute_sha256(receipt_path)
        module = _load_script()
        code = _run(
            module, tmp_path, "--confirmed-by", "operator-a", "--reason", "visual check",
            "--expected-receipt-sha256", expected_sha, "--apply", capsys=capsys,
        )
        assert code == 0
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["match_status"] == "matched"
        assert receipt["match_method"] == "manual_confirmed"
        assert receipt["automatic_decision"]["match_status"] == "ambiguous"
        assert receipt["manual_confirmation"]["confirmed_by"] == "operator-a"
        assert receipt["final_decision"] == {
            "match_status": "matched", "match_method": "manual_confirmed",
        }
        status = json.loads(
            (folder / ".import_status.json").read_text(encoding="utf-8")
        )
        assert status["metadata"]["state"] == "matched"

    def test_receipt_changed_since_review_rejected(
        self, tmp_path: Path, capsys
    ) -> None:
        folder = _ambiguous_workspace(tmp_path)
        module = _load_script()
        code = _run(
            module, tmp_path, "--confirmed-by", "operator-a", "--reason", "visual check",
            "--expected-receipt-sha256", "f" * 64, "--apply", capsys=capsys,
        )
        assert code != 0
        receipt = json.loads(
            (folder / f"{NUMBER}.metadata_match.json").read_text(encoding="utf-8")
        )
        assert receipt["match_status"] == "ambiguous"

    def test_identifier_conflict_never_overridable(self, tmp_path: Path, capsys) -> None:
        folder = tmp_path / NUMBER
        folder.mkdir(parents=True)
        metadata = {
            "title": {"original": "A Study of Snow"},
            "authors": [{"family": "Smith"}],
            "first_author": {"family": "Smith"},
            "year": 2024,
            "identifiers": {"doi": "10.5194/egusphere-2025-5135"},
        }
        (folder / f"{NUMBER}.metadata.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )
        (folder / f"{NUMBER}.pdf").write_bytes(b"%PDF minimal")
        evidence = PdfIdentityEvidence(
            pdf_sha256="",
            doi_evidence=(
                DoiEvidence(
                    doi="10.1007/s10546-021-00629",
                    source="xmp_metadata",
                    page_number=None,
                    labeled=True,
                    context="xmp doi",
                    confidence="strong",
                ),
            ),
            canonical_title="A Completely Different Paper",
            publication_year=2024,
            first_author_family="Zhang",
            author_families=("Zhang",),
            extracted_identifiers=(),
            extraction_sources=("test",),
            confidence="explicit_identifier",
            parser_failures=(),
            warnings=(),
        )
        receipt = build_match_receipt(folder, NUMBER, metadata, evidence)
        assert receipt["match_status"] == "identifier_conflict"
        write_match_receipt(folder, receipt)
        module = _load_script()
        code = _run(
            module, tmp_path, "--confirmed-by", "operator-a", "--reason", "visual check",
            "--apply", capsys=capsys,
        )
        assert code != 0
        receipt = json.loads(
            (folder / f"{NUMBER}.metadata_match.json").read_text(encoding="utf-8")
        )
        assert receipt["match_status"] == "identifier_conflict"

    def test_rerun_same_operator_is_noop(self, tmp_path: Path, capsys) -> None:
        folder = _ambiguous_workspace(tmp_path)
        module = _load_script()
        first = _run(
            module, tmp_path, "--confirmed-by", "operator-a", "--reason", "visual check",
            "--apply", capsys=capsys,
        )
        assert first == 0
        before = json.loads(
            (folder / f"{NUMBER}.metadata_match.json").read_text(encoding="utf-8")
        )
        second = _run(
            module, tmp_path, "--confirmed-by", "operator-a", "--reason", "visual check",
            "--apply", capsys=capsys,
        )
        assert second == 0
        after = json.loads(
            (folder / f"{NUMBER}.metadata_match.json").read_text(encoding="utf-8")
        )
        assert before == after
        assert after["final_decision"]["match_method"] == "manual_confirmed"
