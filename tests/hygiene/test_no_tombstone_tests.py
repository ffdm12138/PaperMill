"""Hygiene guard — no tombstone files (*._deleted) may exist in the repo.

Tombstone files are leftover from test migration and must be deleted, not
kept as `*.py._deleted` markers. They pollute the snapshot, confuse agents,
and are not tracked by git. This test ensures they never re-enter the repo.
"""
from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.hygiene

ROOT = Path(__file__).resolve().parents[2]


def test_no_tombstone_files_anywhere():
    """No `*._deleted` or `*.py._deleted` files may exist anywhere in the repo."""
    bad: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        # Skip .git directory contents
        if ".git" in path.parts:
            continue
        name = path.name
        if name.endswith("._deleted") or name.endswith(".py._deleted"):
            bad.append(str(path.relative_to(ROOT)).replace("\\", "/"))

    assert not bad, f"Found {len(bad)} tombstone file(s) — delete them:\n" + "\n".join(
        f"  - {b}" for b in bad
    )


def test_no_reasonix_state_in_repo():
    """`.reasonix/` is local agent/research tooling state — must not be committed
    or tracked. This catches accidental `git add` of the directory."""
    reasonix = ROOT / ".reasonix"
    # The directory may exist on disk (local tooling), but must be gitignored
    # and must never appear in git tracking.
    result = __import__("subprocess").run(
        ["git", "-C", str(ROOT), "ls-files", "--cached", ".reasonix/"],
        capture_output=True, text=True, check=False,
    )
    tracked = [l for l in result.stdout.splitlines() if l.strip()]
    assert not tracked, (
        f".reasonix/ is git-tracked — add to .gitignore and untrack:\n"
        + "\n".join(f"  - {t}" for t in tracked)
    )
