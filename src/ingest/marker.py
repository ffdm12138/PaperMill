"""Canonical paper-number marker parsing and writing."""
from __future__ import annotations

import json
from pathlib import Path

from src.services.ingest_ids import PAPER_NUMBER_RE
from src.utils.atomic_io import atomic_write_json


def parse_marker_number(marker: str | Path) -> str | None:
    path = Path(marker)
    suffix = ".paper.number"
    if not path.name.endswith(suffix):
        return None
    value = path.name[:-len(suffix)]
    return value if PAPER_NUMBER_RE.fullmatch(value) else None


def write_paper_number_marker(
    folder: str | Path,
    paper_number: str,
    *,
    state: str,
    planned_paper_name: str = "",
) -> Path:
    if not PAPER_NUMBER_RE.fullmatch(str(paper_number)):
        raise ValueError(f"invalid paper_number: {paper_number}")
    root = Path(folder)
    for marker in root.glob("*.paper.number"):
        if marker.name != f"{paper_number}.paper.number":
            marker.unlink()
    path = root / f"{paper_number}.paper.number"
    atomic_write_json(path, {
        "schema_version": "1.0", "paper_number": paper_number,
        "folder_name": root.name, "state": state,
        "planned_paper_name": planned_paper_name,
    }, indent=2)
    return path




