"""Hygiene — encoding, BOM, and mojibake guards (no flat-test dependency)."""
from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent.parent
BOM = b"\xef\xbb\xbf"

MOJIBAKE_PATTERNS = [
    "涓",       # common GBK→UTF-8 mojibake
    "锟斤拷",    # classic mojibake
]


def _collect_source_files() -> list[Path]:
    paths: list[Path] = []
    for pattern in ["tests/**/*.py", "scripts/**/*.py", "src/**/*.py",
                    "*.md", "docs/**/*.md", "tests/**/*.json"]:
        paths.extend(ROOT.glob(pattern))
    return [
        p for p in paths
        if ".git" not in p.parts
        and "__pycache__" not in p.parts
        and ".pytest_cache" not in p.parts
    ]


@pytest.mark.hygiene
def test_python_and_markdown_files_have_no_utf8_bom():
    offenders = []
    for path in _collect_source_files():
        try:
            with open(path, "rb") as f:
                head = f.read(3)
            if head == BOM:
                offenders.append(str(path.relative_to(ROOT)))
        except Exception:
            pass
    assert not offenders, f"files with UTF-8 BOM: {offenders}"


@pytest.mark.hygiene
def test_no_mojibake_in_test_files():
    offenders: list[str] = []
    for path in sorted((ROOT / "tests").glob("**/*.py")):
        if "test_no_bom_or_mojibake" in str(path) or "test_legacy_" in str(path) or "test_test_suite_hygiene" in str(path):
            continue
        if ".git" in str(path) or "__pycache__" in str(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for pattern in MOJIBAKE_PATTERNS:
            if pattern in text:
                offenders.append(f"{path.relative_to(ROOT)}: {pattern!r}")
    assert not offenders, f"mojibake found: {offenders}"


@pytest.mark.hygiene
def test_no_commented_out_asserts_in_test_files():
    offenders: list[str] = []
    for path in sorted((ROOT / "tests").glob("**/*.py")):
        if "test_no_bom_or_mojibake" in str(path) or "test_legacy_" in str(path) or "test_test_suite_hygiene" in str(path):
            continue
        if ".git" in str(path) or "__pycache__" in str(path):
            continue
        try:
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.lstrip()
                if stripped.startswith("#") and "assert " in stripped:
                    if stripped.startswith("# ") and (
                        "check" in stripped.lower()
                        or "guard" in stripped.lower()
                        or "test" in stripped.lower()
                    ):
                        continue
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {stripped[:80]}")
        except Exception:
            pass
    assert not offenders, f"commented-out asserts found: {offenders}"
