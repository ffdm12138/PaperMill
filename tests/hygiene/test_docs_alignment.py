"""Hygiene guard — AGENTS.md and CLAUDE.md must be identical in pairs.

Root `AGENTS.md` / `CLAUDE.md` must match. Each skill directory that has both
files must also have them identical. This prevents drift between the two
canonical agent-instruction files.
"""
from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.hygiene

ROOT = Path(__file__).resolve().parents[2]


def _skill_dirs_with_both() -> list[Path]:
    """Return skill directories that have both AGENTS.md and CLAUDE.md."""
    skills = ROOT / "skills"
    if not skills.is_dir():
        return []
    result = []
    for d in sorted(skills.iterdir()):
        if not d.is_dir():
            continue
        if (d / "AGENTS.md").exists() and (d / "CLAUDE.md").exists():
            result.append(d)
    return result


def test_root_agents_matches_claude():
    """Root AGENTS.md must be byte-identical to CLAUDE.md."""
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert agents == claude, (
        "Root AGENTS.md and CLAUDE.md differ — they must be identical. "
        "Copy the more complete one over the other."
    )


def test_skill_agents_matches_claude():
    """Each skill's AGENTS.md must be byte-identical to its CLAUDE.md."""
    mismatches: list[str] = []
    for skill_dir in _skill_dirs_with_both():
        agents = (skill_dir / "AGENTS.md").read_text(encoding="utf-8")
        claude = (skill_dir / "CLAUDE.md").read_text(encoding="utf-8")
        if agents != claude:
            mismatches.append(str(skill_dir.relative_to(ROOT)).replace("\\", "/"))

    assert not mismatches, (
        f"AGENTS.md / CLAUDE.md differ in skill dirs: {mismatches}. "
        "Copy the more complete one over the other."
    )
