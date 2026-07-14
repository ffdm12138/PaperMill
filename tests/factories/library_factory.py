from __future__ import annotations

import json
from pathlib import Path

from tests.factories.catalog_factory import make_minimal_catalog
from tests.factories.metadata_factory import make_minimal_metadata
from tests.factories.pdf_factory import write_fake_pdf
from tests.factories.source_record_factory import write_metadata_source_record
from src.library.paper_number_ledger import PaperNumberLedger


def make_minimal_paths(tmp_path: Path) -> dict[str, Path]:
    """Return tmp-isolated library paths used by ingest tests."""
    return {
        "paper_raw_dir": tmp_path / "paper_raw",
        "papers_dir": tmp_path / "papers",
        "catalog_dir": tmp_path / "catalog",
        "ledger_path": tmp_path / "catalog" / "paper_number_ledger.json",
    }


def make_paper_raw_item(
    tmp_path: Path,
    *,
    paper_number: str = "0000000000000001",
    with_pdf: bool = True,
    with_metadata: bool = True,
    with_catalog: bool = True,
) -> Path:
    """Create a minimal 16-digit paper_raw workspace under tmp_path."""
    folder = tmp_path / "paper_raw" / paper_number
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{paper_number}.md").write_text("# Example Paper\n\nBody.\n", encoding="utf-8")
    (folder / "images").mkdir(exist_ok=True)
    PaperNumberLedger(tmp_path / "catalog" / "paper_number_ledger.json").reserve_specific_for_paper_raw(
        paper_number,
        folder,
    )
    if with_pdf:
        write_fake_pdf(folder / f"{paper_number}.pdf")
    if with_metadata:
        write_metadata_source_record(folder, "manual")
        metadata = make_minimal_metadata(paper_number=paper_number)
        (folder / f"{paper_number}.metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if with_catalog:
        catalog = make_minimal_catalog(paper_number=paper_number)
        (folder / f"{paper_number}.catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return folder
