"""Hygiene guard: 6-digit paper_number IDs must only appear in tests/legacy/.
Ordinary tests (contract/unit/integration/e2e/hygiene) must use 16-digit
paper_numbers per the v2.3 contract.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.hygiene]

ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = ROOT / "tests"

# Allowed directories where 6-digit IDs are expected.
ALLOWED_DIRS = {
    "tests/legacy",
    "tests/unit/test_pack_repo_rules.py",  # forbidden-path tests may reference legacy
    "tests/hygiene/test_no_legacy_writing_workflow.py",  # legacy-path exclusion guards
}

# Pattern: a bare 6-digit number that looks like a paper_number.
# e.g. "000001", "000042" — but NOT inside a 16-digit number like "0000000000000001".
_LEGACY_ID_RE = re.compile(r"(?<!\d)0{5}\d{1}(?!\d)")


def _scan_file(path: Path) -> list[str]:
    """Return lines containing legacy 6-digit IDs, ignoring comments."""
    hits: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return hits
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        # Skip comment-only lines and docstrings
        if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
            continue
        if _LEGACY_ID_RE.search(line):
            hits.append(f"  {path.relative_to(ROOT)}:{lineno}: {stripped[:120]}")
    return hits


def test_no_legacy_ids_in_ordinary_tests():
    """6-digit paper_numbers must not appear in non-legacy test directories."""
    errors: list[str] = []
    ordinary_dirs = ["contract", "unit", "integration", "e2e", "hygiene"]

    for subdir in ordinary_dirs:
        d = TESTS_DIR / subdir
        if not d.is_dir():
            continue
        for py_file in sorted(d.rglob("test_*.py")):
            rel = str(py_file.relative_to(ROOT)).replace("\\", "/")
            if rel in ALLOWED_DIRS:
                continue
            hits = _scan_file(py_file)
            errors.extend(hits)

    if errors:
        pytest.fail(
            f"Found {len(errors)} legacy 6-digit paper_number(s) outside tests/legacy/:\n"
            + "\n".join(errors[:50])
        )
