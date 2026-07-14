"""Shared hygiene-scanning utilities.

Provides common directory traversal, text-file iteration, and
pattern-matching helpers used by individual hygiene test files.

Usage
-----
Each hygiene test file imports the helpers it needs and specifies its
own scan roots, forbidden tokens/regexes, and allowlists::

    from tests.hygiene._scanner import iter_text_files, scan_tokens, assert_no_matches

    SCAN_ROOTS = ["src", "scripts"]
    FORBIDDEN = ["old_api_v1", "legacy_pivot"]

    def test_no_legacy_terms():
        matches = scan_tokens(iter_text_files(SCAN_ROOTS), FORBIDDEN)
        assert_no_matches(matches, "legacy terms found")
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable


# Suffixes that are never scanned (binary, generated, third-party).
EXCLUDED_SUFFIXES: frozenset[str] = frozenset({
    ".pyc", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
    ".zip", ".tar", ".gz", ".7z", ".rar",
    ".pdf", ".docx", ".xlsx", ".pptx",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".ico",
})


def iter_text_files(
    roots: list[str],
    *,
    excluded_paths: set[str] | None = None,
    excluded_suffixes: frozenset[str] | None = None,
) -> Iterable[tuple[Path, str]]:
    """Yield ``(path, relative_path)`` for every text file under *roots*.

    Parameters
    ----------
    roots:
        Top-level directories or files to scan (relative to repo root).
    excluded_paths:
        Explicit relative paths to skip (e.g. ``{"docs/archive/README.md"}``).
    excluded_suffixes:
        File extensions to skip.  Defaults to ``EXCLUDED_SUFFIXES``.
    """
    root_dir = Path(".")
    excl = excluded_paths or set()
    suffixes = EXCLUDED_SUFFIXES if excluded_suffixes is None else excluded_suffixes
    for root in roots:
        p = root_dir / root
        if not p.exists():
            continue
        if p.is_file():
            if p.suffix.lower() not in suffixes:
                rel = str(p.as_posix())
                if rel not in excl:
                    yield p, rel
        else:
            for f in p.rglob("*"):
                if f.is_dir():
                    continue
                if f.suffix.lower() in suffixes:
                    continue
                rel = str(f.relative_to(root_dir).as_posix())
                if rel in excl:
                    continue
                yield f, rel


def scan_tokens(
    files: Iterable[tuple[Path, str]],
    tokens: Iterable[str],
    *,
    case_sensitive: bool = True,
) -> list[tuple[str, str, str]]:
    """Scan *files* for any occurrence of *tokens*.

    Returns
    -------
    List of ``(relative_path, token, line_snippet)`` triples.
    """
    results: list[tuple[str, str, str]] = []
    for path, rel in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for token in tokens:
            flags = 0 if case_sensitive else re.IGNORECASE
            for m in re.finditer(re.escape(token), text, flags):
                start = max(0, m.start() - 20)
                end = min(len(text), m.end() + 20)
                snippet = text[start:end].replace("\n", " ").strip()
                results.append((rel, token, snippet))
    return results


def scan_regex(
    files: Iterable[tuple[Path, str]],
    patterns: Iterable[str],
    *,
    case_sensitive: bool = True,
) -> list[tuple[str, str, str]]:
    """Like ``scan_tokens`` but each *patterns* entry is a regex."""
    results: list[tuple[str, str, str]] = []
    for path, rel in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for pat in patterns:
            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                compiled = re.compile(pat, flags)
            except re.error:
                continue
            for m in compiled.finditer(text):
                start = max(0, m.start() - 20)
                end = min(len(text), m.end() + 20)
                snippet = text[start:end].replace("\n", " ").strip()
                results.append((rel, pat, snippet))
    return results


def assert_no_matches(
    matches: list[tuple[str, str, str]],
    message: str = "forbidden tokens found",
) -> None:
    """Assert that *matches* is empty, printing each match on failure."""
    if not matches:
        return
    lines = [f"-- {message} --"]
    for rel, token, snippet in sorted(set(matches), key=lambda x: (x[0], x[1])):
        lines.append(f"  {rel}: {token!r} near {snippet!r}")
    raise AssertionError("\n".join(lines))


def assert_allowlist_paths_exist(
    allowlist: set[str],
    *,
    message: str = "allowlist paths not found",
) -> None:
    """Assert that every path in *allowlist* exists on the filesystem."""
    missing = [p for p in sorted(allowlist) if not Path(p).exists()]
    if missing:
        lines = [f"-- {message} --"] + [f"  {p}" for p in missing]
        raise AssertionError("\n".join(lines))
