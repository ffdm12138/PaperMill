"""Hygiene guard — no root flat tests (tests/test_*.py) may exist.

All test coverage must live under the layered architecture:
``tests/contract/``, ``tests/hygiene/``, ``tests/unit/``,
``tests/integration/``, ``tests/e2e/``. Root flat tests are a legacy
structure that must not reappear after migration.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.hygiene

ROOT = Path(__file__).resolve().parents[2]


def test_no_root_flat_tests():
    """No ``tests/test_*.py`` files may exist at the tests/ root.

    All tests must be organized into layered subdirectories. This guard
    prevents old-style flat tests from re-entering the repository.
    """
    bad = sorted((ROOT / "tests").glob("test_*.py"))
    bad_rel = [str(p.relative_to(ROOT)).replace("\\", "/") for p in bad]
    assert not bad_rel, (
        f"Found {len(bad_rel)} root flat test(s) — migrate to a layered "
        f"directory (contract/hygiene/unit/integration/e2e):\n"
        + "\n".join(f"  - {b}" for b in bad_rel)
    )
