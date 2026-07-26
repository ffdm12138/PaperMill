from __future__ import annotations

from pathlib import Path

import pytest

from src.catalog_folders.assignment import valid_decisions
from src.catalog_folders.formal_registry import FormalPaper
from src.catalog_folders.link_backend import create_paper_link, inspect_paper_link, remove_paper_link
from src.catalog_folders.models import CLASSIFIER_SKILL_VERSION, Category
from src.catalog_folders.registry import (
    category_from_notebook, definition_hash, validate_catalog_keyword,
)
from src.discovery.contracts.notebook import notebook_path
from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore
from src.utils.file_fingerprint import compute_sha256


def _write_v3_notebook(
    notebook_dir: Path,
    *,
    search_queries: list[dict[str, str]],
) -> Path:
    store = KeywordNotebookStore(notebook_dir)
    store.ensure_notebook("风吹雪")
    store.sync_search_queries(
        "风吹雪",
        add=search_queries,
        reason="test_fixture",
        operator="pytest",
    )
    return notebook_path("风吹雪", notebook_dir)


def test_validate_catalog_keyword_requires_chinese():
    assert validate_catalog_keyword("风吹雪") == "风吹雪"
    with pytest.raises(ValueError, match="Chinese character"):
        validate_catalog_keyword("snow_drift")
    with pytest.raises(ValueError, match="empty"):
        validate_catalog_keyword("")
    with pytest.raises(ValueError, match="collides with reserved name"):
        validate_catalog_keyword("all")
    with pytest.raises(ValueError, match="collides with reserved name"):
        validate_catalog_keyword("_pending")


def test_validate_catalog_keyword_strict_rejects_whitespace():
    with pytest.raises(ValueError, match="leading/trailing whitespace"):
        validate_catalog_keyword(" 风吹雪")


def test_validate_catalog_keyword_strict_rejects_trailing_dots():
    with pytest.raises(ValueError, match="trailing dots"):
        validate_catalog_keyword("风吹雪...")


def test_validate_catalog_keyword_strict_rejects_path_separators():
    with pytest.raises(ValueError, match="path separators"):
        validate_catalog_keyword("风/雪")


def test_validate_catalog_keyword_strict_rejects_non_nfc():
    # U+00F1 (Latin small n with tilde) composed form vs decomposed
    with pytest.raises(ValueError, match="NFC-normalized"):
        validate_catalog_keyword("café风")  # combining accent = not NFC


def test_validate_catalog_keyword_strict_rejects_windows_reserved():
    with pytest.raises(ValueError, match="Windows reserved name"):
        validate_catalog_keyword("CON")


def test_notebook_category_uses_keyword_as_directory_name(tmp_path: Path):
    notebook = _write_v3_notebook(
        tmp_path,
        search_queries=[
            {"query": "风吹雪", "language": "zh", "source": "canonical"},
            {"query": "风致雪漂移", "language": "zh", "source": "curated"},
            {"query": "blowing snow", "language": "en", "source": "curated"},
            {"query": "snow drift", "language": "en", "source": "curated"},
        ],
    )
    category = category_from_notebook(notebook)
    assert category.keyword_zh == "风吹雪"
    assert category.directory_name == "风吹雪"  # no __id suffix
    assert category.source_notebook == notebook.name


def test_english_search_query_is_not_a_catalog_identity(tmp_path: Path):
    notebook = _write_v3_notebook(
        tmp_path,
        search_queries=[
            {"query": "blowing snow", "language": "en", "source": "curated"},
        ],
    )
    category = category_from_notebook(notebook)
    assert category.keyword_zh == "风吹雪"
    assert category.directory_name != "blowing snow"
    assert "search_queries" not in category.to_dict()


def test_category_directory_name_equals_keyword_by_contract():
    """category_from_notebook enforces directory_name == keyword_zh."""
    cat = Category(
        category_id="a1b2c3d4e5f6a7b8",
        keyword_zh="风吹雪",
        directory_name="风吹雪",
        source_notebook="x.json",
        definition_sha256="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    assert cat.keyword_zh == cat.directory_name


def test_assignment_positive_negative_and_hash_invalidation(tmp_path: Path):
    folder = tmp_path / "paper"
    folder.mkdir()
    catalog = folder / "paper.catalog.json"
    catalog.write_text("{}", encoding="utf-8")
    paper = FormalPaper(
        "0000000000000001", "paper", folder, catalog,
        folder / "paper.metadata.json",
    )
    base = {
        "category_id": "a1b2c3d4e5f6a7b8",
        "keyword_zh": "风吹雪",
        "directory_name": "风吹雪",
        "source_notebook": "x.json",
        "definition_sha256": definition_hash({"category_id": "a1b2c3d4e5f6a7b8", "keyword_zh": "风吹雪"}),
    }
    category = Category(**base)
    assignment = {
        "schema_version": "1.0",
        "paper_number": paper.paper_number,
        "paper_name": paper.paper_name,
        "catalog_sha256": compute_sha256(catalog),
        "decisions": {
            category.category_id: {
                "category_definition_sha256": category.definition_sha256,
                "matched": False,
                "classifier_skill_version": CLASSIFIER_SKILL_VERSION,
            },
        },
    }
    assert valid_decisions(assignment, paper, [category])[category.category_id]["matched"] is False
    catalog.write_text('{"changed":true}', encoding="utf-8")
    assert valid_decisions(assignment, paper, [category]) == {}


def test_directory_link_remove_never_deletes_target(tmp_path: Path):
    target = tmp_path / "papers" / "paper"
    target.mkdir(parents=True)
    sentinel = target / "sentinel"
    sentinel.write_text("safe", encoding="utf-8")
    link = tmp_path / "catalog" / "all" / "0000000000000001"
    created = create_paper_link(link, target)
    assert inspect_paper_link(link).target == target.resolve()
    assert created.kind in {"symlink", "junction"}
    remove_paper_link(link)
    assert sentinel.read_text(encoding="utf-8") == "safe"
    assert not link.exists()


def test_refuses_unmanaged_directory_removal(tmp_path: Path):
    path = tmp_path / "ordinary"
    path.mkdir()
    with pytest.raises(ValueError):
        remove_paper_link(path)
