"""Guard against reintroducing legacy metadata/catalog schema terms.

Active code/docs/skills must speak only metadata v2.0 / catalog v3.1 /
all.catalog v3.1 / paper_index v2.0 terminology.  Legacy field names such
as ``short_zh`` (in metadata), bare ``content_title`` (in catalog), version
string ``v1.1`` (on paper_index schema), and migration
function names ``migrate_catalog_to_v2_0`` are forbidden outside of
scripts/legacy/, one-shot migration scripts, and test fixtures that
deliberately exercise legacy→current migration paths.

Negative-lookahead assertions (e.g. ``content_title`` vs ``content_title_zh``)
avoid false-positives on legitimate new fields.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


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
    # Migration function that no longer exists
    ("migrate_catalog_to_v2_0 import/call",
     r'migrate_catalog_to_v2_0'),
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
    "src/services/v2_library.py",
    # Filter code strips short_zh/translated_zh from compact patches.
    "src/services/metadata_resolver.py",
    # Repair/validate scripts reference legacy terms to detect and remove them.
    "scripts/repair_metadata_only_assets.py",
    "scripts/validate_metadata_only_assets.py",
    # Patch-schema description explicitly lists the forbidden legacy
    # fields (it is telling the LLM not to generate them).
    "skills/paper_raw_metadata_resolver/metadata_patch_schema.json",
    # One-shot migration that processes legacy keys on purpose.
    # NOTE: migrate_metadata_catalog_to_current.py was moved to scripts/legacy/
    # (deprecated; its catalog part KeyErrors on v3.1). scripts/legacy/ is
    # covered by EXEMPT_DIR_PREFIXES, so no explicit entry is needed here.
    # Tex-writer example uses "schema_version": "1.0" for the write-job
    # wrapper schema (not catalog/metadata).  This is the live constant.
    "skills/catalog_tex_writer/examples/example_selected_catalog.json",
    # paper_number_ledger is its own schema (different from catalog/metadata).
    # Its current version "1.0" matches the live code constant at
    # src/services/v2_library.py:1840 — not a legacy residual.
    "data/catalog/paper_number_ledger.template.json",
}

# Test files that legitimately use legacy fields in fixtures for
# migration tests or validator-rejection tests.
EXEMPT_TEST_FILES: set[str] = {
    "tests/legacy/test_legacy_repair_bad_formal_imports.py",
    "tests/legacy/test_legacy_paper_raw_formal_import_audit.py",
    "tests/integration/test_v2_library.py",
    "tests/contract/test_catalog_metadata_separation.py",
    "tests/contract/test_safe_delete_and_paper_id.py",
    # This file itself may contain the terms in its docstring.
    "tests/contract/test_schema_contract_no_legacy_terms.py",
}

EXEMPT_SUFFIXES = {".pyc", ".png", ".jpg", ".jpeg", ".pdf", ".zip", ".gif", ".svg"}


def _scan_files() -> list[Path]:
    out: list[Path] = []
    for top in ("AGENTS.md", "README.md", "CLAUDE.md", "docs", "src", "scripts", "skills"):
        p = REPO / top
        if not p.exists():
            continue
        if p.is_file():
            if p.suffix in EXEMPT_SUFFIXES:
                continue
            out.append(p)
            continue
        for child in p.rglob("*"):
            rel = str(child.relative_to(REPO)).replace("\\", "/")
            if not child.is_file():
                continue
            if any(rel.startswith(prefix) for prefix in EXEMPT_DIR_PREFIXES):
                continue
            if child.suffix in EXEMPT_SUFFIXES:
                continue
            out.append(child)
    # data/: only check template files, not real generated data
    data_template_root = REPO / "data"
    if data_template_root.exists():
        for child in data_template_root.rglob("*.template.json"):
            out.append(child)
        for child in data_template_root.rglob("*.template.yaml"):
            out.append(child)
    return out


def test_no_legacy_schema_terms():
    offenders: list[str] = []
    for path in _scan_files():
        rel = str(path.relative_to(REPO)).replace("\\", "/")
        if rel in EXEMPT_FILES:
            continue
        if rel.startswith("tests/") and rel in EXEMPT_TEST_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in FORBIDDEN_PATTERNS:
            matches = re.findall(pattern, text)
            if matches:
                offenders.append(f"{rel}: {label} (found: {matches[0]})")
    assert not offenders, (
        "Legacy schema terms found in active code/docs/skills. "
        "Remove them or move to scripts/legacy/:\n" +
        "\n".join(sorted(offenders))
    )
