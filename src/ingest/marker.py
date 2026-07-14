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


def read_paper_number_marker(path: str | Path) -> dict:
    marker = Path(path)
    value = json.loads(marker.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("paper-number marker must be a JSON object")
    number = parse_marker_number(marker)
    if not number or value.get("paper_number") != number:
        raise ValueError("paper-number marker identity mismatch")
    return value
