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

from tests.hygiene._scanner import (
    assert_allowlist_paths_exist,
    iter_text_files,
    scan_tokens,
)


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

LEGACY_ONLY_ALLOWED = set()

SCAN_DIRS = ["src", "scripts", "config", "web", "skills", "docs"]
UNSAFE_ONE_OFF_ROOT_SCRIPTS = {
    "post_compact_fix.py",
    "restore_missing_ledger.py",
    "fix_duplicated_marker.py",
}


def test_no_forbidden_v1_legacy_tokens():
    files = iter_text_files(SCAN_DIRS, excluded_suffixes={".pyc"})
    matches = scan_tokens(files, FORBIDDEN_TOKENS)
    offenders = [
        f"{rel}: {token}"
        for rel, token, _ in matches
        if rel not in LEGACY_ONLY_ALLOWED and "LEGACY MIGRATION SCRIPT" not in rel
    ]
    assert not offenders, "forbidden v1 tokens found:\n" + "\n".join(offenders)


def test_unsafe_one_off_repair_scripts_not_in_scripts_root():
    offenders = [
        f"scripts/{name}"
        for name in sorted(UNSAFE_ONE_OFF_ROOT_SCRIPTS)
        if (ROOT / "scripts" / name).exists()
    ]
    assert not offenders, "unsafe one-off repair scripts remain in scripts root:\n" + "\n".join(offenders)


# ── Legacy source-id allocation must not appear in normal ingest ───────

_NORMAL_INGEST_ALLOWED = {
    "tests/hygiene/test_no_legacy_mainflow_refs.py",
}


def test_normal_ingest_no_planned_source_id_or_six_digit_allocator():
    forbidden = ("planned_source_id", ":06d}", "_TEMP_ID_RE.match(p.name)")
    files = iter_text_files(["src", "scripts", "tests"])
    matches = scan_tokens(files, forbidden)
    offenders: list[str] = []
    for rel, token, _ in matches:
        rel_posix = rel.replace("\\", "/")
        if rel_posix in _NORMAL_INGEST_ALLOWED or "__pycache__" in rel_posix:
            continue
        if rel_posix.startswith("scripts/legacy/"):
            continue
        if "LEGACY MIGRATION SCRIPT" in rel:
            continue
        offenders.append(f"{rel_posix}: {token}")
    assert not offenders, "normal ingest still references legacy source-id allocation:\n" + "\n".join(offenders)


# ── Active code must not reference legacy field/asset names ───────────

def test_no_legacy_field_paths_in_active_code():
    """Active code must not reference legacy field/asset names."""
    files = iter_text_files(SCAN_DIRS, excluded_suffixes={".pyc"})
    paper_md_matches = scan_tokens(files, ["paper.md"])
    paper_md_offenders = [
        rel for rel, _, _ in paper_md_matches
        if rel.replace("\\", "/") != "scripts/validate_v2_library.py"
    ]
    assert not paper_md_offenders, "paper.md referenced as a path in:\n" + "\n".join(paper_md_offenders)

    citation_offenders: list[str] = []
    for sub in ("src/writer", "src/bib.py"):
        base = ROOT / sub
        paths = [base] if base.is_file() else list(base.rglob("*.py"))
        for path in paths:
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if '"citation"' in text or "'citation'" in text or '.get("citation"' in text:
                citation_offenders.append(str(path.relative_to(ROOT)))
    assert not citation_offenders, "legacy citation field still read in:\n" + "\n".join(citation_offenders)


# ── Normal tests must not handwrite formalize artifacts ───────────────

_READY_ARTIFACT_ALLOWED = {
    "tests/contract/test_metadata_quality_audit.py",
    # This file itself contains the scan literals.
    "tests/hygiene/test_no_legacy_mainflow_refs.py",
    # reconcile fixtures build legacy/corpse workspaces with markers by hand
    # pack_repo workspace sampling tests create *.paper.number markers as
    # test fixtures for sampling rules — these are not formalize artifacts.
    "tests/unit/test_pack_repo_rules.py",
    # Concurrency regression fixtures intentionally build minimal numeric
    # workspaces and durable commit journals in tmp_path.
    "tests/e2e/test_transaction_concurrency.py",
    "tests/contract/test_ingest_state_machine.py",
    # formal registry error tests build minimal formal layouts for negative cases
    "tests/unit/test_formal_registry_errors.py",
    # validate_v2_library tests create formal fixtures with markers
    "tests/integration/test_validate_v2_library.py",
    # Lifecycle state-machine tests below intentionally exercise marker recovery.
    "tests/integration/test_discovery_index_unsettled_refresh.py",
}


def test_normal_tests_do_not_handwrite_ready_for_commit_artifacts():
    assert_allowlist_paths_exist(
        _READY_ARTIFACT_ALLOWED,
        message="ready-for-commit allowlist paths missing",
    )
    tokens = ("ready_for_commit", ".paper.number", "formalization.json")
    write_ops = (".write_text(", "atomic_write_json(", "_write_json(")
    offenders: list[str] = []
    for path in (ROOT / "tests").rglob("*.py"):
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        if rel in _READY_ARTIFACT_ALLOWED or "__pycache__" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if any(token in line for token in tokens) and any(op in line for op in write_ops):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, "normal tests handwrite formalize artifacts:\n" + "\n".join(offenders)


# ── Legacy ledger repoint must not be called ──────────────────────────

def test_formalize_main_path_does_not_call_legacy_repoint():
    files = iter_text_files(["src", "scripts", "tests"])
    legacy_repoint = "ledger." + "repoint("
    self_legacy_repoint = "self.ledger." + "repoint("
    matches = scan_tokens(files, [legacy_repoint, self_legacy_repoint])
    offenders = [rel for rel, _, _ in matches]
    assert not offenders, "legacy ledger repoint call remains:\n" + "\n".join(offenders)


# ── Markdown front-matter rule must stay first 100 lines ─────────────

def test_markdown_front_matter_rule_stays_first_100_lines():
    paths = list(iter_text_files(["src", "scripts", "tests", "docs", "skills"]))
    haystack = "\n".join(
        path.read_text(encoding="utf-8")
        for path, rel in paths
        if rel.replace("\\", "/") != "tests/hygiene/test_no_legacy_mainflow_refs.py"
    )
    assert "first 10 lines" not in haystack
    assert "前 10 行" not in haystack
    assert "first 100" in haystack or "前 100" in haystack or "max_lines=100" in haystack
