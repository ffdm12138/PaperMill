"""Hygiene guard — ``.import_status.json`` has exactly one locked write path.

CLAUDE.md: the file may be changed only through the canonical locked
read-modify-write service.  The engine is ``src/ingest/status.py`` (nested
status v2) and the sole facade is ``src/ingest/import_status.py`` (flat
translation + routing).  No other src module may define a writer or obtain
``write_import_status`` from anywhere else.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENGINE = "src/ingest/status.py"
FACADE = "src/ingest/import_status.py"

pytestmark = pytest.mark.hygiene


def _src_files() -> list[Path]:
    return sorted((ROOT / "src").rglob("*.py"))


def test_only_facade_defines_write_import_status():
    offenders = []
    for path in _src_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel == FACADE:
            continue
        if re.search(r"^def write_import_status\(", path.read_text(encoding="utf-8"), re.M):
            offenders.append(rel)
    assert not offenders, f"extra write_import_status definitions: {offenders}"


def test_only_engine_defines_nested_status_writers():
    offenders = []
    for path in _src_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel == ENGINE:
            continue
        text = path.read_text(encoding="utf-8")
        for name in ("def update_import_status(", "def initialize_status(", "def update_status("):
            if name in text:
                offenders.append(f"{rel}: {name}")
    assert not offenders, f"nested status writers outside the engine: {offenders}"


def test_writer_callers_import_from_the_facade():
    """Every src module calling write_import_status must import it from the facade."""
    offenders = []
    for path in _src_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in (FACADE, ENGINE):
            continue
        text = path.read_text(encoding="utf-8")
        if "write_import_status(" not in text:
            continue
        if "from src.ingest.import_status import" not in text:
            offenders.append(rel)
    assert not offenders, (
        f"write_import_status called without importing the facade: {offenders}"
    )
