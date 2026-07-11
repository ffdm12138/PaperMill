"""Current numeric paper_raw conversion fixture helpers."""
from __future__ import annotations

import json
from pathlib import Path

from src.library.paper_number_ledger import PaperNumberLedger
from src.metadata.schema import empty_metadata


def make_staged_source(tmp_path: Path, paper_number: str = "0000000000000001") -> Path:
    folder = tmp_path / "paper_raw" / paper_number
    folder.mkdir(parents=True)
    PaperNumberLedger(tmp_path / "catalog" / "paper_number_ledger.json").reserve_specific_for_paper_raw(paper_number, folder)
    metadata = empty_metadata(paper_number)
    (folder / f"{paper_number}.metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (folder / f"{paper_number}.pdf").write_bytes(b"%PDF synthetic")
    (folder / f"{paper_number}.md").write_text("# Synthetic conversion", encoding="utf-8")
    (folder / "images").mkdir()
    (folder / f"{paper_number}.conversion.json").write_text(json.dumps({"paper_number": paper_number}), encoding="utf-8")
    return folder
