from pathlib import Path

import pytest

from tests.hygiene._scanner import (
    assert_allowlist_paths_exist,
    iter_text_files,
    scan_tokens,
)


pytestmark = pytest.mark.contract
ROOT = Path(__file__).resolve().parents[2]
ACTIVE_ROOTS = [
    "src", "scripts", "skills", "config", "docs", "README.md",
    "CLAUDE.md", "AGENTS.md", "tests",
]
FORBIDDEN = (
    "research" + "_card",
    "Catalog v" + "3.1",
    "catalog_" + "v31",
    "migrate_" + "catalog",
    "legacy_catalog_" + "v31",
    "one_sentence" + "_summary_zh",
    "main_conclusion" + "_zh",
    "usefulness_for_project" + "_zh",
    "relevance" + "_score",
    "classification.topic" + "_tags",
    "screening.relevance" + "_score",
    "commit_" + "workspace =",
)


def test_retired_architecture_is_absent_from_every_active_directory():
    allowed_rejection = ROOT / "tests" / "contract" / "test_catalog_contract.py"
    files = [
        (path, rel)
        for path, rel in iter_text_files(ACTIVE_ROOTS, excluded_suffixes={".pyc"})
        if path != Path(__file__)
    ]
    matches = scan_tokens(files, FORBIDDEN)
    offenders = [
        f"{rel}: {token}"
        for path, rel, token, _ in matches
        if not (path == allowed_rejection and token == FORBIDDEN[0])
    ]
    assert offenders == []


_REMOVED_PATHS = (
    "src/migration",
    "src/catalog/migration.py",
    "scripts/migrate_" + "catalog_v3_1_to_v3_2.py",
    "scripts/migrate_paper_raw_to_numbered_workspaces.py",
    "tests/integration/test_catalog_migration_v32.py",
    "tests/integration/test_raw_workspace_migration.py",
)


def test_removed_schema_and_workspace_migration_paths_do_not_exist():
    for relative in _REMOVED_PATHS:
        assert not (ROOT / relative).exists()



def test_new_metadata_schema_has_no_embedded_match_field():
    from src.metadata.schema import empty_metadata

    assert "metadata_match" not in empty_metadata("0000000000000001")
