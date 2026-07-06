"""Hygiene checks for shell syntax mislabeling in Markdown documentation.

Guards against the recurring bug of writing Windows cmd.exe ``set VAR=value``
inside a ```bash fenced code block, which misleads agents running in Git Bash
into using the wrong environment-variable syntax. Also enforces that formal
SOP commands using ``smoke_mineru_conversion.py --paper-number`` carry
``--apply`` (readiness-only diagnostics are an explicit, labeled exception).
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent.parent

# Core docs to scan (avoid fragile full-tree scans of audits/archives).
_CORE_DOCS: list[str] = [
    "AGENTS.md",
    "CLAUDE.md",
    "docs/DEPENDENCIES_AND_EXTERNAL_TOOLS.md",
    "docs/MINERU_PERFORMANCE_PLAN.md",
    "docs/ARCHITECTURE.md",
    "docs/SCRIPT_USAGE.md",
    "docs/PROJECT_STATUS.md",
    "docs/PROJECT_CONTRACT.md",
    "skills/literature_library_manager/SKILL.md",
    "skills/literature_library_manager/AGENTS.md",
    "skills/literature_library_manager/CLAUDE.md",
    "skills/literature_library_manager/README.md",
]

_SET_VAR_RE = re.compile(r"^\s*set\s+[A-Z_]+=")
_SMOKE_CMD_RE = re.compile(r"smoke_mineru_conversion\.py\s+--paper-number\s+\S+")


def _extract_fenced_blocks(text: str) -> list[tuple[str, list[str], int]]:
    """Return (lang, lines, start_line) for each fenced code block."""
    lines = text.split("\n")
    blocks: list[tuple[str, list[str], int]] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("```"):
            lang = stripped[3:].strip().lower()
            start = i + 1
            i += 1
            block: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            blocks.append((lang, block, start))
            i += 1
        else:
            i += 1
    return blocks


def test_no_cmd_set_in_bash_fenced_blocks():
    """bash/sh/shell fenced blocks must not contain ``set VAR=value``."""
    offenders: list[str] = []
    for rel in _CORE_DOCS:
        path = ROOT / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for lang, block, start in _extract_fenced_blocks(text):
            if lang in ("bash", "sh", "shell"):
                for offset, line in enumerate(block):
                    if _SET_VAR_RE.match(line):
                        offenders.append(f"{rel}:{start + offset} [{lang}] {line.strip()}")
    assert not offenders, (
        "cmd.exe `set VAR=value` found inside bash/sh/shell fenced blocks "
        "(use ```bat for cmd.exe, or `export VAR=value` for bash):\n" + "\n".join(offenders)
    )


def test_formal_sop_smoke_commands_carry_apply():
    """Formal SOP smoke commands must carry --apply.

    Readiness-only diagnostic mentions are exempt only when the surrounding
    context explicitly labels them as readiness-only (within 3 lines).
    """
    violations: list[str] = []
    readiness_markers = ("readiness-only", "readiness only", "without --apply")
    for rel in _CORE_DOCS:
        path = ROOT / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if not _SMOKE_CMD_RE.search(line):
                continue
            if "--apply" in line:
                continue
            # Check if this line is within a readiness-only labeled context.
            context = "\n".join(lines[max(0, i - 3):i + 4]).lower()
            if any(marker in context for marker in readiness_markers):
                continue
            # Skip descriptive prose (line doesn't look like a runnable command).
            if not re.search(r"(python|conda run|--report)", line):
                continue
            violations.append(f"{rel}:{i + 1} {line.strip()[:100]}")
    assert not violations, (
        "smoke_mineru_conversion.py --paper-number commands in formal SOP "
        "must carry --apply (readiness-only diagnostics must be explicitly "
        "labeled):\n" + "\n".join(violations)
    )


def test_bat_files_not_scanned():
    """Sanity: .bat files are not markdown and must not be checked here."""
    bat = ROOT / "start_fast_api_mode.bat"
    if bat.exists():
        text = bat.read_text(encoding="utf-8", errors="replace")
        assert "set CUDA_VISIBLE_DEVICES=0" in text
