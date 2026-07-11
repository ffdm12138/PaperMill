from __future__ import annotations
import json
from pathlib import Path
from src.metadata.citation_readiness import validate_citation_ready
from src.metadata.pdf_match import canonical_title
from src.metadata.pdf_match import build_match_receipt
from src.metadata.pdf_identity import PdfIdentityEvidence
from src.metadata.freeze import freeze_metadata
from src.catalog.schema import truncate_summary
from src.ingest.transactions import CommitJournalStore

def _meta():
    return {"entry_type":"article","title":{"original":"A title: Subtitle"},"authors":[{"family":"Wang","given":"A"}],"year":2024,"container":{"journal":"J"},"identifiers":{"doi":"10.1/test"},"links":{"url":""}}
def test_citation_readiness_is_metadata_only():
    assert validate_citation_ready(_meta()).ready
def test_canonical_title_preserves_subtitle():
    assert canonical_title("A title: Subtitle") != canonical_title("A title")
def test_doi_conflict_cannot_downgrade_to_title_match(tmp_path: Path):
    folder=tmp_path; (folder/"0000000000000001.metadata.json").write_text(json.dumps(_meta()),encoding="utf-8"); (folder/"0000000000000001.pdf").write_bytes(b"pdf")
    evidence=PdfIdentityEvidence("",("10.2/conflict",),"a title subtitle",2024,"Wang",("Wang",),("test",),"explicit_identifier",())
    receipt=build_match_receipt(folder,"0000000000000001",_meta(),evidence)
    assert receipt["match_status"] == "identifier_conflict"
def test_manual_confirmation_requires_auditable_fields(tmp_path: Path):
    folder=tmp_path; (folder/"0000000000000001.metadata.json").write_text(json.dumps(_meta()),encoding="utf-8"); (folder/"0000000000000001.pdf").write_bytes(b"pdf")
    evidence=PdfIdentityEvidence("",(),"a title subtitle",2024,"Wang",("Wang",),("test",),"structured_front_matter",())
    receipt=build_match_receipt(folder,"0000000000000001",_meta(),evidence,manual={"operator":"","reason":"","evidence":[]})
    assert receipt["match_status"] == "mismatch"
def test_projection_truncation_is_deterministic():
    text="甲。"*400; out=truncate_summary(text); assert len(out)<=600 and out.endswith("…")
def test_journal_is_external_and_single_active(tmp_path: Path):
    source=tmp_path/"raw"; source.mkdir(); (source/"0000000000000001.metadata.json").write_text("{}",encoding="utf-8"); (source/"0000000000000001.catalog.json").write_text("{}",encoding="utf-8"); formal=source/"plan.json"; formal.write_text("{}",encoding="utf-8")
    store=CommitJournalStore(tmp_path/"transactions"); one=store.create(paper_number="0000000000000001",paper_id="2024_Wang_测试",source=source,staging=tmp_path/"stg",final=tmp_path/"final",formalization=formal)
    assert "/raw/" not in str(store.active); assert store.find_active("0000000000000001")
    try: store.create(paper_number="0000000000000001",paper_id="2024_Wang_测试",source=source,staging=tmp_path/"x",final=tmp_path/"y",formalization=formal)
    except RuntimeError: pass
    else: raise AssertionError("duplicate active transaction accepted")
