from __future__ import annotations

import pytest

from src.ingest.paper_raw import PaperRawAllocator
from tests.factories.pdf_factory import write_fake_pdf


pytestmark = pytest.mark.e2e


def test_minimal_manual_ingest_smoke_stays_in_tmp_path(tmp_path):
    raw_pdf = write_fake_pdf(tmp_path / "raw" / "paper.pdf")
    allocator = PaperRawAllocator(
        tmp_path / "paper_raw",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        papers_dir=tmp_path / "papers",
    )

    result = allocator.allocate_from_pdf(raw_pdf, source_type="manual_pdf", move=True)

    assert result["paper_number"] == "0000000000000001"
    assert str(result["folder"]).startswith(str(tmp_path))
