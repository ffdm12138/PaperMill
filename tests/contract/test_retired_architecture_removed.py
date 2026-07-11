from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ACTIVE_ROOTS = (
    "src", "scripts", "skills", "config", "docs", "README.md",
    "CLAUDE.md", "AGENTS.md", "tests",
)
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


def _active_files():
    for name in ACTIVE_ROOTS:
        root = ROOT / name
        if root.is_file():
            yield root
        elif root.exists():
            yield from (path for path in root.rglob("*") if path.is_file())


def test_retired_architecture_is_absent_from_every_active_directory():
    offenders = []
    allowed_rejection = ROOT / "tests" / "contract" / "test_catalog_contract.py"
    for path in _active_files():
        if path == Path(__file__) or path.suffix.lower() in {".pyc", ".zip", ".png", ".jpg", ".pdf"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in FORBIDDEN:
            if token in text and not (path == allowed_rejection and token == FORBIDDEN[0]):
                offenders.append(f"{path.relative_to(ROOT).as_posix()}: {token}")
    assert offenders == []


def test_removed_schema_and_workspace_migration_paths_do_not_exist():
    for relative in (
        "src/migration",
        "src/catalog/migration.py",
        "scripts/migrate_" + "catalog_v3_1_to_v3_2.py",
        "scripts/migrate_paper_raw_to_numbered_workspaces.py",
        "tests/integration/test_catalog_migration_v32.py",
        "tests/integration/test_raw_workspace_migration.py",
    ):
        assert not (ROOT / relative).exists()


def test_agent_instruction_files_are_byte_identical():
    assert (ROOT / "CLAUDE.md").read_bytes() == (ROOT / "AGENTS.md").read_bytes()


def test_new_metadata_schema_has_no_embedded_match_field():
    from src.metadata.schema import empty_metadata

    assert "metadata_match" not in empty_metadata("0000000000000001")
