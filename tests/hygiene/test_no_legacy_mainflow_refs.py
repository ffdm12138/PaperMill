"""Hygiene guard — deprecated/legacy concepts must not appear on active paths.

Consolidates guards that were previously pinned inside
``tests/legacy/test_legacy_cleanup_grep.py``. The active codebase, active docs,
and ordinary tests must not reference legacy v1 concepts, deprecated migration
functions, or handwrite formalize artifacts.

Each guard here is unique (not covered by test_no_scihub,
test_no_legacy_writing_workflow, test_no_legacy_ids_outside_legacy, or
test_schema_contract_no_legacy_terms, which scan for different token sets).
"""
from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.hygiene

ROOT = Path(__file__).resolve().parents[2]

# ── Deprecated scripts must not be re-hosted on active paths ──────────

def test_deprecated_migrate_metadata_catalog_not_in_active_one_shot_dir():
    """The deprecated catalog/metadata migration script must not be re-hosted
    in the active one_shot_migrations directory."""
    active = ROOT / "scripts" / "one_shot_migrations" \
        / "migrate_metadata_catalog_to_current.py"
    assert not active.exists(), (
        f"deprecated script must not live in one_shot_migrations: {active}"
    )


# ── Forbidden v1 concept tokens in active code/docs ───────────────────

FORBIDDEN_TOKENS = [
    "papers_pdf",
    "register_manual_pdf",
    "import_pending_pdf",
    "library_index",
    "identity_index",
    "domain_catalog",
    "domain_library",
    "literature_catalog",
    "ai_summary",
    "relevance_to_my_work",
]

LEGACY_ONLY_ALLOWED = {
    "scripts/legacy/fix_paper_ids_batch.py",
    "scripts/legacy/fix_paperid_case.py",
    "scripts/legacy/fix_remaining_rename.py",
    "scripts/legacy/ingest_ids.py",
    "scripts/legacy/audit_paper_raw_formal_imports.py",
    "scripts/legacy/migrate_paper_raw_6digit_to_paper_number.py",
    "scripts/legacy/rename_english_papers.py",
    "scripts/legacy/rename_raw_to_paperid.py",
}

SCAN_DIRS = ["src", "scripts", "config", "web", "skills", "docs"]
UNSAFE_ONE_OFF_ROOT_SCRIPTS = {
    "post_compact_fix.py",
    "restore_missing_ledger.py",
    "fix_duplicated_marker.py",
}


def _source_files() -> list[Path]:
    out: list[Path] = []
    for sub in SCAN_DIRS:
        base = ROOT / sub
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            if "__pycache__" in p.parts:
                continue
            if p.suffix in {".pyc", ".png", ".jpg", ".jpeg", ".pdf", ".zip"}:
                continue
            out.append(p)
    return out


def test_no_forbidden_v1_legacy_tokens():
    offenders: list[str] = []
    for path in _source_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for token in FORBIDDEN_TOKENS:
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)}: {token}")
        if "legacy-only" in text:
            rel = str(path.relative_to(ROOT)).replace("\\", "/")
            if rel not in LEGACY_ONLY_ALLOWED and "LEGACY MIGRATION SCRIPT" not in text:
                offenders.append(f"{rel}: legacy-only")
    assert not offenders, "forbidden v1 tokens found:\n" + "\n".join(offenders)


def test_unsafe_one_off_repair_scripts_not_in_scripts_root():
    offenders = [
        f"scripts/{name}"
        for name in sorted(UNSAFE_ONE_OFF_ROOT_SCRIPTS)
        if (ROOT / "scripts" / name).exists()
    ]
    assert not offenders, "unsafe one-off repair scripts remain in scripts root:\n" + "\n".join(offenders)


def test_normal_ingest_no_planned_source_id_or_six_digit_allocator():
    offenders: list[str] = []
    allowed = {
        "tests/legacy/test_legacy_migrate_paper_raw_6digit.py",
        # This file itself contains the forbidden tokens as scan literals.
        "tests/hygiene/test_no_legacy_mainflow_refs.py",
    }
    forbidden = ("planned_source_id", ":06d}", "_TEMP_ID_RE.match(p.name)")
    for sub in ("src", "scripts", "tests"):
        for path in (ROOT / sub).rglob("*.py"):
            rel = str(path.relative_to(ROOT)).replace("\\", "/")
            if rel in allowed or "__pycache__" in path.parts:
                continue
            if rel.startswith("scripts/legacy/"):
                continue
            text = path.read_text(encoding="utf-8")
            if "LEGACY MIGRATION SCRIPT" in text:
                continue
            for token in forbidden:
                if token in text:
                    offenders.append(f"{rel}: {token}")
    assert not offenders, "normal ingest still references legacy source-id allocation:\n" + "\n".join(offenders)


def test_no_legacy_field_paths_in_active_code():
    """Active code must not reference legacy field/asset names.

    Consolidates: ``paper.md`` must not appear as a formal asset path (except
    in validate_v2_library.py which detects/rejects it), and src/writer +
    src/bib must not read a flat ``citation`` field on catalog entries.
    """
    paper_md_offenders: list[str] = []
    for path in _source_files():
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        if rel == "scripts/validate_v2_library.py":
            continue  # legitimately detects/rejects paper.md
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "paper.md" in text:
            paper_md_offenders.append(rel)
    assert not paper_md_offenders, "paper.md referenced as a path in:\n" + "\n".join(paper_md_offenders)

    citation_offenders: list[str] = []
    for sub in ("src/writer", "src/bib.py"):
        base = ROOT / sub
        paths = [base] if base.is_file() else list(base.rglob("*.py"))
        for path in paths:
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if '"citation"' in text or "'citation'" in text or ".get(\"citation\"" in text:
                citation_offenders.append(str(path.relative_to(ROOT)))
    assert not citation_offenders, "legacy citation field still read in:\n" + "\n".join(citation_offenders)


def test_normal_tests_do_not_handwrite_ready_for_commit_artifacts():
    allowed = {
        "tests/helpers/paper_raw_factory.py",
        "tests/contract/test_catalog_metadata_separation.py",
        "tests/legacy/test_legacy_paper_raw_formal_import_audit.py",
        "tests/contract/test_metadata_quality_audit.py",
        "tests/legacy/test_legacy_repair_bad_formal_imports.py",
        "tests/integration/test_paper_raw_commit_atomic.py",
        "tests/integration/test_v2_library.py",
        "tests/integration/test_paper_number_admin.py",
        # reconcile fixtures build legacy/corpse workspaces with markers by hand
        "tests/slow/test_reconcile_paper_raw_non_destructive.py",
        # malformed _ready_dirs gate negative case
        "tests/contract/test_manual_import_metadata_requirements.py",
        # _formal_paper fixture hand-writes a *.paper.number marker so the
        # fixture is a complete formal layout (catalog-schema negatives etc.)
        "tests/contract/test_catalog_repository_state.py",
        # This file itself contains the tokens/write_ops as scan literals.
        "tests/hygiene/test_no_legacy_mainflow_refs.py",
        # pack_repo workspace sampling tests create *.paper.number markers as
        # test fixtures for sampling rules — these are not formalize artifacts.
        "tests/unit/test_pack_repo_rules.py",
    }
    tokens = ("ready_for_commit", ".paper.number", "formalization.json")
    write_ops = (".write_text(", "atomic_write_json(", "_write_json(")
    offenders: list[str] = []
    for path in (ROOT / "tests").rglob("*.py"):
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        if rel in allowed or "__pycache__" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if any(token in line for token in tokens) and any(op in line for op in write_ops):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, "normal tests handwrite formalize artifacts:\n" + "\n".join(offenders)


def test_formalize_main_path_does_not_call_legacy_repoint():
    offenders: list[str] = []
    for sub in ("src", "scripts", "tests"):
        base = ROOT / sub
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            rel = str(path.relative_to(ROOT)).replace("\\", "/")
            if rel == "tests/hygiene/test_no_legacy_mainflow_refs.py":
                continue  # this file contains the scan literals
            text = path.read_text(encoding="utf-8")
            legacy_repoint = "ledger." + "repoint("
            self_legacy_repoint = "self.ledger." + "repoint("
            if legacy_repoint in text or self_legacy_repoint in text:
                offenders.append(rel)
    assert not offenders, "legacy ledger repoint call remains:\n" + "\n".join(offenders)


def test_markdown_front_matter_rule_stays_first_100_lines():
    paths = [p for sub in ("src", "scripts", "tests", "docs", "skills") for p in (ROOT / sub).rglob("*") if p.is_file()]
    haystack = "\n".join(
        path.read_text(encoding="utf-8")
        for path in paths
        if str(path.relative_to(ROOT)).replace("\\", "/") != "tests/hygiene/test_no_legacy_mainflow_refs.py"
        and path.suffix not in {".pyc", ".png", ".jpg", ".jpeg", ".pdf", ".zip"}
    )
    assert "first 10 lines" not in haystack
    assert "前 10 行" not in haystack
    assert "first 100" in haystack or "前 100" in haystack or "max_lines=100" in haystack
