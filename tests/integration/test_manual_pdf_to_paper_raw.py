from __future__ import annotations

import json

import pytest

from src.services.v2_library import PaperRawAllocator
from tests.factories.pdf_factory import write_fake_pdf


pytestmark = pytest.mark.integration


def test_manual_pdf_stages_16_digit_workspace_with_source_record(tmp_path):
    raw_pdf = write_fake_pdf(tmp_path / "raw" / "manual.pdf")
    allocator = PaperRawAllocator(
        tmp_path / "paper_raw",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        papers_dir=tmp_path / "papers",
    )

    result = allocator.allocate_from_pdf(raw_pdf, source_type="manual_pdf", move=True)

    paper_number = result["paper_number"]
    folder = tmp_path / "paper_raw" / paper_number
    assert paper_number == "0000000000000001"
    assert folder.is_dir()
    assert (folder / f"{paper_number}.pdf").exists()
    assert (folder / f"{paper_number}.paper.number").exists()
    metadata = json.loads((folder / f"{paper_number}.metadata.json").read_text(encoding="utf-8"))
    assert metadata["source"]["provider"] == "manual"
    assert metadata["source"]["raw_record_path"] == "source_records/metadata_source.manual.json"
    assert not raw_pdf.exists()
