"""Static validation for start.bat.

Ensures the Windows launcher only references scripts that actually exist and
never points to retired entry points such as the watcher or deleted ingest CLIs.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


pytestmark = pytest.mark.hygiene

ROOT = Path(__file__).resolve().parents[2]
START_BAT = ROOT / "start.bat"


_DELETED_SCRIPTS = {
    "match_paper_raw_metadata.py",
    "watcher.py",
    "write_review.py",
    "repair_catalog_asset_refs.py",
    "manage_catalog_categories.py",
    "rebuild_catalog_folder_system.py",
}


def _py_refs(text: str) -> set[str]:
    """Return relative paths to .py files referenced in the batch file."""
    refs: set[str] = set()
    # python -m src.server -> src/server.py
    for m in re.finditer(r"\bpython\s+-m\s+([\w.]+)", text):
        refs.add(m.group(1).replace(".", "/") + ".py")
    # scripts\start_mineru_services.py -> scripts/start_mineru_services.py
    for m in re.finditer(r"[\w./\\]+\w+\.py", text):
        path = m.group(0).replace("\\", "/")
        refs.add(path)
    return refs


def test_start_bat_exists_and_references_real_scripts():
    assert START_BAT.exists(), "start.bat is missing"
    text = START_BAT.read_text(encoding="utf-8")
    refs = _py_refs(text)
    missing = [ref for ref in sorted(refs) if not (ROOT / ref).exists()]
    assert not missing, f"start.bat references missing Python files: {missing}"


def test_start_bat_does_not_reference_deleted_scripts():
    text = START_BAT.read_text(encoding="utf-8")
    offenders = [name for name in _DELETED_SCRIPTS if name in text]
    assert not offenders, f"start.bat references deleted scripts: {offenders}"


def test_start_bat_does_not_reference_watcher():
    text = START_BAT.read_text(encoding="utf-8")
    assert "watcher" not in text.lower(), "start.bat still contains watcher references"
