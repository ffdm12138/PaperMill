"""Guard against reintroducing legacy metadata/catalog/index schema terms.

Active code/docs/skills must speak only metadata v2.0 / Catalog v3.2 /
Catalog-folder terminology.  Legacy field names such as ``short_zh``
(in metadata), bare ``content_title`` (in catalog), and retired index
references (``all.catalog``, ``paper_index``) are forbidden.

Negative-lookahead assertions (e.g. ``content_title`` vs ``content_title_zh``)
avoid false-positives on legitimate new fields.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.hygiene._scanner import (
    assert_allowlist_paths_exist,
    iter_text_files,
    scan_regex,
)


pytestmark = pytest.mark.contract

REPO = Path(__file__).resolve().parents[2]

# ── forbidden patterns ──────────────────────────────────────────────
# Each pattern is a ``(label, regex_str)`` pair so error messages are
# readable even when the regex escapes are hard to scan.

FORBIDDEN_PATTERNS: list[tuple[str, str]] = [
    # Legacy field names used in metadata positions
    ("short_zh in metadata/title",
     r'"short_zh"'),
    ("translated_zh in metadata/title",
     r'"translated_zh"'),
    # Legacy bare content_title (NOT content_title_zh)
    ("bare content_title (non-_zh) in catalog",
     r'"content_title"(?!_zh)'),
    # Assertions that still mention old schema versions
    ("catalog schema v2.0 assertion",
     r'catalog schema 保持 v2\.0'),
    ("catalog (schema v2.0) assertion",
     r'catalog（schema v2\.0）'),
    ("catalog（v2.0）in active docs",
     r'catalog（v2\.0'),
    ("content-only catalog (v2.0) in active docs",
     r'content-only catalog \(v2\.0\)'),
    ("per-paper catalog schema v2.0",
     r'per-paper catalog schema v2\.0'),
    ("metadata schema v1.1 assertion",
     r'metadata schema.*v1\.1'),
    ("all.catalog schema v2.0 assertion",
     r'all\.catalog schema v2\.0'),
    # paper_index is the only schema that previously used "1.1" and is now "2.0".
    ("paper_index schema_version 1.1",
     r'"schema_version": "1\.1"'),
]

# ── files / paths that are EXEMPT from scanning ─────────────────────

EXEMPT_DIR_PREFIXES = {
    "scripts/legacy",
    "docs/audits",
    "docs/archive",
}

EXEMPT_FILES: set[str] = {
    # Validator code legitimately references short_zh/translated_zh as
    # forbidden-key constants (enforcement, not usage).
    "src/metadata/schema.py",
    # Filter code strips short_zh/translated_zh from compact patches.
    "src/metadata_resolve/sidecars.py",
    # Patch-schema description explicitly lists the forbidden legacy
    # fields (it is telling the LLM not to generate them).
    "skills/paper_raw_metadata_resolver/metadata_patch_schema.json",
    # Tex-writer example uses "schema_version": "1.0" for the write-job
    # wrapper schema (not catalog/metadata).  This is the live constant.
    "skills/catalog_tex_writer/examples/example_selected_catalog.json",
}

# Test files that legitimately use retired fields in validator-rejection tests.
EXEMPT_TEST_FILES: set[str] = {
    # This file itself may contain the terms in its docstring.
    "tests/contract/test_schema_contract_no_legacy_terms.py",
}


def _should_scan(rel: str) -> bool:
    rel_posix = rel.replace("\\", "/")
    if any(rel_posix.startswith(prefix) for prefix in EXEMPT_DIR_PREFIXES):
        return False
    if rel_posix in EXEMPT_FILES:
        return False
    if rel_posix.startswith("tests/") and rel_posix in EXEMPT_TEST_FILES:
        return False
    return True


SCAN_ROOTS = ["AGENTS.md", "README.md", "CLAUDE.md", "docs", "src", "scripts", "skills"]


def test_no_legacy_schema_terms():
    assert_allowlist_paths_exist(
        EXEMPT_FILES | EXEMPT_TEST_FILES,
        message="schema-contract allowlist paths missing",
    )
    files = [
        (path, rel)
        for path, rel in iter_text_files(SCAN_ROOTS, excluded_suffixes={".pyc"})
        if _should_scan(rel)
    ]
    labels = [label for label, _ in FORBIDDEN_PATTERNS]
    patterns = [pattern for _, pattern in FORBIDDEN_PATTERNS]
    matches = scan_regex(files, patterns)
    offenders: list[str] = []
    for rel, pattern, snippet in matches:
        for label, pat in FORBIDDEN_PATTERNS:
            if pat == pattern:
                offenders.append(f"{rel}: {label} (found: {snippet[:40]!r})")
                break
    assert not offenders, (
        "Legacy schema terms found in active code/docs/skills. "
        "Remove them or move to scripts/legacy/:\n" +
        "\n".join(sorted(offenders))
    )
