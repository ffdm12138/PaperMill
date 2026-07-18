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
    "doi_evidence" + "_fetcher",
    "_network" + "_fetcher",
    "_group" + "_metrics",
    "validate" + "_sampling_profile",
    "_looks" + "_like_page_fetcher",
    "_legacy" + "_sample",
    "fetch" + "_shared_corpus",
    "budget_" + "key",
    # Discovery v2 cleanup — removed zero-call functions and aliases
    "extract_doi" + "_from_sidecar",
    "enrich_from" + "_sidecar",
    "formal_" + "dois",
    "formal_pdf" + "_shas",
    "move_reserved_workspace" + "_for_migration",
    "load_legacy_registry" + "_for_migration",
    "recover_apply" + "_journals",
    "request_observation" + "_hash",
    "get_bibtex" + "_by_doi",
    "lookup_by_paper" + "_number",
    "file_" + "meta",
    "read_paper_number" + "_marker",
    "sanitize_url_" + "fields",
    "unsettled_paper_raw" + "_numbers",
    "complete_for_metadata" + "_staged",
    "only_preflight" + "_ready",
    # Notebook v3 migration — removed subsystem
    "notebook_v3_mig" + "ration\\.py",
    "migrate_keyword_note" + "books_v3\\.py",
    # v83 cleanup — removed not_configured_resolvers field
    "not_configured" + "_resolvers",
    # v83 cleanup — removed MinerU legacy output cache fallback
    "_find_" + "legacy",
    "_legacy_pdf" + "_index",
    "_locate_legacy" + "_assets",
    "legacy_output" + "_roots",
    # v83 cleanup — removed retired notebook scan from production
    "scan_retired_" + "notebooks",
    "keyword_notebooks" + "_retired",
    # v83 cleanup — removed hanging compat aliases
    "PAGE_ALL_V2" + "_FIELDS",
    "PAGE_REQUIRED_V2" + "_FIELDS",
)


def test_retired_architecture_is_absent_from_every_active_directory():
    allowed_rejection = ROOT / "tests" / "contract" / "test_catalog_contract.py"
    files = [
        (path, rel)
        for path, rel in iter_text_files(ACTIVE_ROOTS, excluded_suffixes={".pyc"})
        if path.resolve() != Path(__file__).resolve()
    ]
    matches = scan_tokens(files, FORBIDDEN)
    offenders = [
        f"{rel}: {token}"
        for rel, token, _ in matches
        if not ((ROOT / rel).resolve() == allowed_rejection and token == FORBIDDEN[0])
    ]
    assert offenders == []


_REMOVED_PATHS = (
    "src/migration",
    "src/catalog/migration.py",
    "scripts/migrate_" + "catalog_v3_1_to_v3_2.py",
    "scripts/migrate_paper_raw_to_numbered_workspaces.py",
    "tests/integration/test_catalog_migration_v32.py",
    "tests/integration/test_raw_workspace_migration.py",
    # Notebook v3 migration removed
    "src/discovery/notebook_v3_migration.py",
    "scripts/migrate_keyword_notebooks_v3.py",
    "data/discovery/keyword_notebooks_retired",
)


def test_removed_schema_and_workspace_migration_paths_do_not_exist():
    for relative in _REMOVED_PATHS:
        assert not (ROOT / relative).exists()



def test_new_metadata_schema_has_no_embedded_match_field():
    from src.metadata.schema import empty_metadata

    assert "metadata_match" not in empty_metadata("0000000000000001")
