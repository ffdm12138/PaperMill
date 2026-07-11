from __future__ import annotations
import json
from pathlib import Path
import pytest

from src.metadata.freeze import assert_metadata_frozen,assert_metadata_write_allowed
from src.metadata.pdf_identity import PdfIdentityEvidence
from src.metadata.pdf_match import build_match_receipt,validate_metadata_match_receipt
from tests.integration.test_frozen_v32_transaction_pipeline import NUMBER,_workspace

def test_requested_doi_is_not_pdf_identity(tmp_path: Path):
    folder=tmp_path; metadata={"title":{"original":"A"},"authors":[{"family":"Smith"}],"first_author":{"family":"Smith"},"year":2024,"identifiers":{"doi":"10.1234/a"}}
    meta=folder/f"{NUMBER}.metadata.json"; pdf=folder/f"{NUMBER}.pdf"; meta.write_text(json.dumps(metadata),encoding="utf-8"); pdf.write_bytes(b"%PDF no identifier")
    evidence=PdfIdentityEvidence("",(),None,None,None,(),("pdf",),"missing",())
    receipt=build_match_receipt(folder,NUMBER,metadata,evidence,requested_doi="10.1234/a")
    assert receipt["match_method"]=="mismatch" and receipt["match_status"]=="mismatch"

def test_identifier_conflict_cannot_be_manually_confirmed(tmp_path: Path):
    folder=tmp_path; metadata={"title":{"original":"A"},"authors":[{"family":"Smith"}],"first_author":{"family":"Smith"},"year":2024,"identifiers":{"doi":"10.1234/a"}}
    meta=folder/f"{NUMBER}.metadata.json"; pdf=folder/f"{NUMBER}.pdf"; meta.write_text(json.dumps(metadata),encoding="utf-8"); pdf.write_bytes(b"%PDF")
    evidence=PdfIdentityEvidence("",("10.1234/b",),None,None,None,(),("pdf",),"explicit_identifier",())
    manual={"operator":"admin","reason":"looked","evidence":[{"type":"visual_pdf_inspection","detail":"title page"}],"confirmed_at":"2026-01-01T00:00:00+00:00","metadata_sha256":__import__("src.file_fingerprint",fromlist=["compute_sha256"]).compute_sha256(meta),"pdf_sha256":__import__("src.file_fingerprint",fromlist=["compute_sha256"]).compute_sha256(pdf)}
    receipt=build_match_receipt(folder,NUMBER,metadata,evidence,manual=manual)
    assert receipt["match_method"]=="identifier_conflict"

@pytest.mark.parametrize("asset",["pdf","metadata_match","source_record"])
def test_freeze_guard_replays_entire_evidence_closure(tmp_path: Path,asset: str):
    workspace,_,_,_=_workspace(tmp_path)
    if asset=="pdf": workspace.pdf.write_bytes(workspace.pdf.read_bytes()+b"tamper")
    elif asset=="metadata_match":
        value=json.loads(workspace.metadata_match.read_text(encoding="utf-8")); value["match_status"]="mismatch"; workspace.metadata_match.write_text(json.dumps(value),encoding="utf-8")
    else:
        record=next(workspace.source_records.glob("*.json")); record.write_text("{}",encoding="utf-8")
    with pytest.raises(ValueError): assert_metadata_frozen(workspace.root,NUMBER)

def test_frozen_metadata_rejects_normal_writer(tmp_path: Path):
    workspace,_,_,_=_workspace(tmp_path)
    with pytest.raises(PermissionError): assert_metadata_write_allowed(workspace.root,NUMBER)
