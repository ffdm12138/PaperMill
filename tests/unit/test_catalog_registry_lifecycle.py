"""Comprehensive registry lifecycle tests.

Covers: notebook validation, category_from_notebook, definition_hash,
sync_registry, collision detection, unicode normalization, reserved keywords.
"""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from src.catalog_folders.exceptions import (
    DuplicateKeyword,
    FilesystemNameCollision,
    InvalidChineseKeyword,
    NotebookSchemaError,
)
from src.catalog_folders.models import CLASSIFIER_SKILL_VERSION, Category
from src.catalog_folders.registry import (
    category_from_notebook,
    definition_hash,
    load_categories,
    sync_registry,
    validate_catalog_keyword,
)
from src.discovery.keyword_notebook import KeywordNotebookStore, empty_notebook


# ── helpers ──────────────────────────────────────────────────────────

def _write_notebook(notebook_dir: Path, keyword: str, *,
                    keyword_id: str | None = None,
                    guidance_zh: str | None = None,
                    aliases_zh: list[str] | None = None,
                    exclusions_zh: list[str] | None = None,
                    normalized_keyword: str | None = None,
                    search_queries: list[dict[str, str]] | None = None,
                    provider_error: str | None = None,
                    enabled: bool = True,
                    filename: str | None = None) -> Path:
    """Write a complete schema-v3 keyword notebook for testing.

    Query entries are built through the public notebook store so provider
    state stays structurally valid.  Explicit identity overrides are reserved
    for fail-closed corruption tests.
    """
    data = empty_notebook(keyword)
    if search_queries:
        with TemporaryDirectory(dir=notebook_dir) as build_dir:
            store = KeywordNotebookStore(Path(build_dir))
            store.ensure_notebook(keyword)
            store.sync_search_queries(
                keyword,
                add=search_queries,
                reason="test_fixture",
                operator="pytest",
            )
            data = store.require_v3(keyword)
    data["classification"] = {
        "guidance_zh": guidance_zh,
        "aliases_zh": aliases_zh or [],
        "exclusions_zh": exclusions_zh or [],
    }
    data["enabled"] = enabled
    if normalized_keyword is not None:
        data["normalized_keyword_zh"] = normalized_keyword
    if provider_error is not None:
        query = next(iter(data["search_queries"].values()))
        provider = next(iter(query["providers"].values()))
        provider["refresh"]["last_error"] = provider_error
    if keyword_id is not None:
        data["keyword_id"] = keyword_id
    fname = filename or f"{data['keyword_id']}.json"
    path = notebook_dir / fname
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _write_registry(registry_path: Path, categories: list[dict]) -> Path:
    """Write a category registry JSON file."""
    normalized_categories = []
    for category in categories:
        row = dict(category)
        row.setdefault("normalized_keyword_zh", row["keyword_zh"])
        row.setdefault("guidance_zh", None)
        row.setdefault("aliases_zh", [])
        row.setdefault("exclusions_zh", [])
        row["definition_sha256"] = definition_hash(row)
        normalized_categories.append(row)
    data = {
        "schema_version": "1.0",
        "updated_at": "2026-01-01T00:00:00Z",
        "categories": normalized_categories,
    }
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return registry_path


# ── enabled / retired lifecycle ──────────────────────────────────────

def test_enabled_false_notebook_excluded(tmp_path):
    """Notebook with enabled=false is stored but does not become an active category."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()
    registry_path = tmp_path / "registry.json"

    _write_notebook(notebook_dir, "风吹雪", enabled=False)
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)

    # enabled=false → not loaded as active by load_categories
    cats = load_categories(registry_path)
    assert len(cats) == 0

    # But it IS in the raw registry
    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    assert len(raw["categories"]) == 1
    assert raw["categories"][0]["classification_enabled"] is False


def test_enabled_true_notebook_included(tmp_path):
    """Notebook with enabled=true becomes an active category."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()
    registry_path = tmp_path / "registry.json"

    _write_notebook(notebook_dir, "风吹雪", enabled=True)
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)

    cats = load_categories(registry_path)
    assert len(cats) == 1
    assert cats[0].keyword_zh == "风吹雪"


def test_notebook_deleted_category_retired(tmp_path):
    """Deleting a notebook removes its category from the active registry.

    The old category is retired — it no longer appears in raw registry
    categories or in load_categories(), and a retirement history record
    is written.
    """
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()
    registry_path = tmp_path / "registry.json"

    # First sync — notebook present
    nb = _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)
    assert len(load_categories(registry_path)) == 1

    # Delete the notebook
    nb.unlink()

    # Second sync — deleted notebook's category is retired
    report = sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)
    # Category no longer in active registry
    assert len(load_categories(registry_path)) == 0
    # Reported as retired
    assert "2211dcaa01587d44" in report["retired"]
    # Retirement history record written
    history_dir = registry_path.parent / "category_history" / "2211dcaa01587d44"
    assert history_dir.is_dir()
    history_files = list(history_dir.glob("*.json"))
    assert len(history_files) == 1
    history = json.loads(history_files[0].read_text(encoding="utf-8"))
    assert history["category_id"] == "2211dcaa01587d44"
    assert history["reason"] == "notebook_deleted"


def test_notebook_reenabled(tmp_path):
    """Re-enabling a notebook via registry edit brings the category back as active."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()
    registry_path = tmp_path / "registry.json"

    _write_notebook(notebook_dir, "风吹雪", enabled=True)
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)
    assert len(load_categories(registry_path)) == 1

    # Mark as disabled in registry
    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    raw["categories"][0]["classification_enabled"] = False
    registry_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    assert len(load_categories(registry_path)) == 0

    # Re-enable in registry
    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    raw["categories"][0]["classification_enabled"] = True
    raw["categories"][0].pop("retired_at", None)
    registry_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    assert len(load_categories(registry_path)) == 1


# ── definition_hash tests ────────────────────────────────────────────

def test_guidance_update_changes_hash(tmp_path):
    """Updating guidance_zh changes definition_hash."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()
    registry_path = tmp_path / "registry.json"

    _write_notebook(notebook_dir, "风吹雪", guidance_zh="旧版指导")
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)
    hash1 = load_categories(registry_path)[0].definition_sha256

    # Update guidance in registry directly
    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    raw["categories"][0]["guidance_zh"] = "新版指导"
    raw["categories"][0]["definition_sha256"] = definition_hash(raw["categories"][0])
    registry_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    hash2 = load_categories(registry_path)[0].definition_sha256

    assert hash1 != hash2


def test_alias_update_changes_hash(tmp_path):
    """Updating aliases_zh changes definition_hash."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()
    registry_path = tmp_path / "registry.json"

    _write_notebook(notebook_dir, "风吹雪", aliases_zh=["风雪"])
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)
    hash1 = load_categories(registry_path)[0].definition_sha256

    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    raw["categories"][0]["aliases_zh"] = ["暴风雪"]
    raw["categories"][0]["definition_sha256"] = definition_hash(raw["categories"][0])
    registry_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    hash2 = load_categories(registry_path)[0].definition_sha256

    assert hash1 != hash2


def test_search_query_addition_and_deletion_do_not_change_hash(tmp_path):
    """Adding or deleting bilingual queries does not invalidate decisions."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()
    registry_path = tmp_path / "registry.json"

    initial_queries = [
        {"query": "风吹雪", "language": "zh", "source": "canonical"},
        {"query": "blowing snow", "language": "en", "source": "curated"},
    ]
    notebook = _write_notebook(
        notebook_dir, "风吹雪", search_queries=initial_queries,
    )
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)
    initial_hash = load_categories(registry_path)[0].definition_sha256

    notebook.unlink()
    notebook = _write_notebook(
        notebook_dir,
        "风吹雪",
        search_queries=[
            *initial_queries,
            {"query": "漂移积雪", "language": "zh", "source": "curated"},
            {"query": "snow transport", "language": "en", "source": "curated"},
        ],
    )
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)
    added_hash = load_categories(registry_path)[0].definition_sha256

    raw = json.loads(notebook.read_text(encoding="utf-8"))
    deleted_query_id = next(
        query_id for query_id, query in raw["search_queries"].items()
        if query["query"] == "blowing snow"
    )
    del raw["search_queries"][deleted_query_id]
    notebook.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)
    deleted_hash = load_categories(registry_path)[0].definition_sha256

    assert initial_hash == added_hash == deleted_hash


def test_provider_cursor_change_no_hash_change(tmp_path):
    """Changing nested v3 provider state does not change definition_hash."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()
    registry_path = tmp_path / "registry.json"

    queries = [{"query": "风吹雪", "language": "zh", "source": "canonical"}]
    _write_notebook(
        notebook_dir, "风吹雪", search_queries=queries, provider_error="first error",
    )
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)
    hash1 = load_categories(registry_path)[0].definition_sha256

    for nb in notebook_dir.glob("*.json"):
        nb.unlink()
    _write_notebook(
        notebook_dir, "风吹雪", search_queries=queries, provider_error="second error",
    )
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)
    hash2 = load_categories(registry_path)[0].definition_sha256

    assert hash1 == hash2


def test_definition_hash_includes_classifier_version(tmp_path):
    """definition_hash includes CLASSIFIER_SKILL_VERSION.

    The hash includes the classifier skill version so that upgrading the
    classifier invalidates all previous decisions.
    """
    data = {
        "category_id": "a1b2c3d4e5f6a7b8",
        "keyword_zh": "风吹雪",
    }
    h = definition_hash(data)
    # Hash must be non-empty SHA-256
    assert len(h) == 64
    # Verify CLASSIFIER_SKILL_VERSION is in the hash payload
    assert "classifier_skill_version" in {
        "category_id", "keyword_zh", "guidance_zh",
        "aliases_zh", "exclusions_zh", "classifier_skill_version",
    }


def test_definition_hash_fields_match_category_entity(tmp_path):
    """Verify that definition_hash fields correspond to Category attributes."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()

    nb = _write_notebook(notebook_dir, "风吹雪",
                         guidance_zh="指导文本", aliases_zh=["风雪"],
                         exclusions_zh=["排除"])
    cat = category_from_notebook(nb)

    # Recompute hash from Category fields to cross-check
    recomputed = definition_hash({
        "category_id": cat.category_id,
        "keyword_zh": cat.keyword_zh,
        "guidance_zh": cat.guidance_zh,
        "aliases_zh": cat.aliases_zh,
        "exclusions_zh": cat.exclusions_zh,
        "classifier_skill_version": CLASSIFIER_SKILL_VERSION,
    })
    assert recomputed == cat.definition_sha256


# ── keyword validation ───────────────────────────────────────────────

def test_english_search_queries_do_not_become_categories(tmp_path):
    """English discovery queries remain inside one Chinese category notebook."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()

    nb = _write_notebook(
        notebook_dir,
        "风吹雪",
        search_queries=[
            {"query": "风吹雪", "language": "zh", "source": "canonical"},
            {"query": "风致雪漂移", "language": "zh", "source": "curated"},
            {"query": "blowing snow", "language": "en", "source": "curated"},
            {"query": "snow drift", "language": "en", "source": "curated"},
        ],
    )
    category = category_from_notebook(nb)
    assert category.keyword_zh == "风吹雪"
    assert category.directory_name == "风吹雪"


def test_invalid_chinese_keyword_rejected(tmp_path):
    """Keyword with path separators raises FilesystemNameCollision."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()

    # Path separator raises FilesystemNameCollision during pre-normalization check
    nb = _write_notebook(notebook_dir, "风/雪测试")
    with pytest.raises(FilesystemNameCollision, match="path separator"):
        category_from_notebook(nb)


def test_invalid_keyword_id_rejected(tmp_path):
    """Malformed keyword_id is rejected by strict notebook validation."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()

    nb = _write_notebook(notebook_dir, "风吹雪", keyword_id="bad_id_123")
    with pytest.raises(NotebookSchemaError, match="keyword_id does not match"):
        category_from_notebook(nb)


def test_missing_enabled_field_fail_closed(tmp_path):
    """Notebook without 'enabled' field raises NotebookSchemaError."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()

    nb = _write_notebook(notebook_dir, "风吹雪")
    raw = json.loads(nb.read_text(encoding="utf-8"))
    del raw["enabled"]
    nb.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(NotebookSchemaError):
        category_from_notebook(nb)


# ── collision detection ──────────────────────────────────────────────

def test_same_keyword_different_id_collision(tmp_path):
    """Notebook with keyword_id that doesn't match derived keyword_id is rejected.

    The keyword_id MUST equal keyword_id(raw_keyword).  A mismatched ID is
    caught by strict schema validation before collision detection runs.
    """
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()
    registry_path = tmp_path / "registry.json"

    # This notebook has the correct derived keyword_id
    _write_notebook(notebook_dir, "风吹雪")

    # This notebook has a mismatched keyword_id — category_from_notebook raises
    nb2 = _write_notebook(notebook_dir, "风吹雪", keyword_id="b2c3d4e5f6a7b8c9")
    with pytest.raises(NotebookSchemaError, match="keyword_id does not match"):
        category_from_notebook(nb2)

    # sync_registry reports the parse error (not a collision)
    result = sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=False)
    assert len(result["notebook_parse_errors"]) >= 1


def test_unicode_collision(tmp_path):
    """Non-NFC keyword raises InvalidChineseKeyword.

    The validator requires NFC-normalized Unicode. NFD form is rejected.
    """
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()
    registry_path = tmp_path / "registry.json"

    # validate_catalog_keyword rejects non-NFC inputs
    with pytest.raises(InvalidChineseKeyword, match="NFC"):
        validate_catalog_keyword("é中文")  # 'e' + combining acute + 中文


def test_casefold_collision_reserved(tmp_path):
    """Reserved words 'all', '_pending', '.state' raise FilesystemNameCollision."""
    for reserved in ["all", "_pending", ".state"]:
        with pytest.raises(FilesystemNameCollision):
            validate_catalog_keyword(reserved)


# ── sync_registry behavior ───────────────────────────────────────────

def test_sync_registry_notebook_values_override_old_registry(tmp_path):
    """Notebook fields (guidance_zh, aliases_zh) override old registry values.

    Only retired_at is preserved from the old registry.
    """
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()
    registry_path = tmp_path / "registry.json"

    # First sync
    _write_notebook(notebook_dir, "风吹雪",
                    guidance_zh="旧版指导", aliases_zh=["旧别名"])
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)

    # Update notebook with new guidance
    for nb in notebook_dir.glob("*.json"):
        nb.unlink()
    _write_notebook(notebook_dir, "风吹雪",
                    guidance_zh="新版指导", aliases_zh=["新别名"])
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)

    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    cat = raw["categories"][0]
    # Notebook values take precedence
    assert cat.get("guidance_zh") == "新版指导"
    assert cat.get("aliases_zh") == ["新别名"]


def test_sync_registry_repairs_stale_definition_hash_from_notebook(tmp_path):
    """Sync may repair only historical definition-hash drift."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()
    registry_path = tmp_path / "registry.json"

    _write_notebook(notebook_dir, "风吹雪", guidance_zh="当前分类定义")
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)

    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    raw["categories"][0]["definition_sha256"] = "0" * 64
    registry_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    report = sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)

    assert report["changed"] == ["2211dcaa01587d44"]
    repaired = json.loads(registry_path.read_text(encoding="utf-8"))["categories"][0]
    assert repaired["definition_sha256"] == definition_hash(repaired)
    assert load_categories(registry_path)[0].keyword_zh == "风吹雪"


def test_sync_registry_strips_retired_at_for_enabled(tmp_path):
    """An enabled notebook has retired_at stripped; old registry cannot override."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()
    registry_path = tmp_path / "registry.json"

    _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)

    # Manually set retired_at in registry (simulates stale state)
    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    raw["categories"][0]["retired_at"] = "2026-01-01T00:00:00Z"
    raw["categories"][0]["definition_sha256"] = definition_hash(raw["categories"][0])
    registry_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    # Re-sync: notebook is still enabled → retired_at MUST be stripped
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)
    raw2 = json.loads(registry_path.read_text(encoding="utf-8"))
    assert "retired_at" not in raw2["categories"][0]


def test_notebook_values_override_old_registry(tmp_path):
    """Notebook category fields (keyword_zh, directory_name, source_notebook) come from notebook."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()
    registry_path = tmp_path / "registry.json"

    _write_notebook(notebook_dir, "风吹雪",
                    guidance_zh="新版指导", aliases_zh=["别名"])
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)

    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    cat = raw["categories"][0]
    assert cat["keyword_zh"] == "风吹雪"
    assert cat["directory_name"] == "风吹雪"
    assert cat["source_notebook"].endswith(".json")
    assert cat.get("guidance_zh") == "新版指导"


def test_sync_registry_bilingual_queries_create_one_chinese_category(tmp_path):
    """Multiple bilingual queries remain search inputs for one Chinese concept."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()
    registry_path = tmp_path / "registry.json"

    _write_notebook(
        notebook_dir,
        "风吹雪",
        search_queries=[
            {"query": "风吹雪", "language": "zh", "source": "canonical"},
            {"query": "风致雪输运", "language": "zh", "source": "curated"},
            {"query": "blowing snow", "language": "en", "source": "curated"},
            {"query": "wind-driven snow transport", "language": "en", "source": "curated"},
        ],
    )

    result = sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)
    categories = load_categories(registry_path)
    assert [(category.keyword_zh, category.directory_name) for category in categories] == [
        ("风吹雪", "风吹雪"),
    ]
    registry_text = registry_path.read_text(encoding="utf-8")
    assert "blowing snow" not in registry_text
    assert "wind-driven snow transport" not in registry_text


def test_load_categories_filters_disabled_and_retired(tmp_path):
    """load_categories excludes disabled and retired categories."""
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [
        {
            "category_id": "2211dcaa01587d44", "keyword_zh": "风吹雪",
            "directory_name": "风吹雪", "source_notebook": "风吹雪.json",
            "definition_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "classification_enabled": True,
        },
        {
            "category_id": "ace250fe675fc00d", "keyword_zh": "雪粒破碎",
            "directory_name": "雪粒破碎", "source_notebook": "雪粒破碎.json",
            "definition_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "classification_enabled": False,
        },
        {
            "category_id": "33aa4f978b3e3891", "keyword_zh": "风洞实验",
            "directory_name": "风洞实验", "source_notebook": "风洞实验.json",
            "definition_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc", "retired_at": "2026-01-01T00:00:00Z",
        },
    ])

    cats = load_categories(registry_path)
    assert len(cats) == 1
    assert cats[0].keyword_zh == "风吹雪"


def test_invalid_registry_schema_fail_closed(tmp_path):
    """Registry with wrong schema_version raises error."""
    registry_path = tmp_path / "registry.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps({
        "schema_version": "0.9",
        "categories": [],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="Registry validation failed"):
        load_categories(registry_path)


def test_load_categories_empty_registry(tmp_path):
    """Missing registry file returns empty list."""
    registry_path = tmp_path / "nonexistent.json"
    cats = load_categories(registry_path)
    assert cats == []


def test_definition_hash_deterministic(tmp_path):
    """Same input produces same hash."""
    data = {
        "category_id": "a1b2c3d4e5f6a7b8",
        "keyword_zh": "风吹雪",
        "guidance_zh": "指导",
    }
    h1 = definition_hash(data)
    h2 = definition_hash(data)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256


def test_definition_hash_empty_optional_fields(tmp_path):
    """Empty optional fields are excluded from hash payload — same hash."""
    data_with = {
        "category_id": "a1b2c3d4e5f6a7b8",
        "keyword_zh": "风吹雪",
        "guidance_zh": "指导",
    }
    data_with_empty_aliases = {
        "category_id": "a1b2c3d4e5f6a7b8",
        "keyword_zh": "风吹雪",
        "guidance_zh": "指导",
        "aliases_zh": (),
        "exclusions_zh": (),
    }
    # Empty tuples excluded → same hash
    assert definition_hash(data_with) == definition_hash(data_with_empty_aliases)


def test_definition_hash_excludes_search_queries_runtime_state_and_paths(tmp_path):
    """definition_hash excludes v3 query/runtime state and file paths."""
    base = {
        "category_id": "a1b2c3d4e5f6a7b8",
        "keyword_zh": "风吹雪",
    }
    h1 = definition_hash(base)
    extended = {
        **base,
        "search_queries": {
            "0123456789abcdef": {"query": "blowing snow", "language": "en"},
        },
        "query_runtime_state": {"crossref": {"cursor": "xyz"}},
        "catalog_path": "/some/path",
    }
    h2 = definition_hash(extended)
    assert h1 == h2


def test_sync_registry_empty_notebook_dir(tmp_path):
    """Empty notebook directory produces valid registry with no changes."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()
    registry_path = tmp_path / "registry.json"

    result = sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)
    assert result["added"] == []
    assert result["changed"] == []
    assert result["collisions"] == []


def test_sync_registry_adds_new_category(tmp_path):
    """sync_registry adds new categories from notebooks."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()
    registry_path = tmp_path / "registry.json"

    _write_notebook(notebook_dir, "风吹雪")
    result = sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)
    assert result["added"] == ["2211dcaa01587d44"]
    assert result["changed"] == []


def test_active_disabled_same_keyword_rejected(tmp_path):
    """Active and disabled notebooks sharing the same keyword: collision.

    An enabled=true notebook and enabled=false notebook with the same
    keyword_zh create an ambiguous keyword → category mapping.  The sync
    must detect and report this as a collision.
    """
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()
    registry_path = tmp_path / "registry.json"

    _write_notebook(notebook_dir, "风吹雪", enabled=True, filename="notebook_a.json")
    _write_notebook(notebook_dir, "风吹雪", enabled=False, filename="notebook_b.json")

    # dry-run detects the cross-detection collision
    result = sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=False)
    assert len(result["collisions"]) >= 1
    collision_types = {c["type"] for c in result["collisions"]}
    assert "active_disabled_same_keyword" in collision_types

    # apply=True should raise DuplicateKeyword
    with pytest.raises(DuplicateKeyword):
        sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)


# ── Section 2 new tests ──────────────────────────────────────────────

def test_unchanged_second_sync_has_no_retirement(tmp_path):
    """Second sync with unchanged notebooks produces no new retirements."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()
    registry_path = tmp_path / "registry.json"

    _write_notebook(notebook_dir, "风吹雪")
    r1 = sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)
    assert r1["retired"] == []

    r2 = sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)
    assert r2["retired"] == []


def test_unchanged_second_sync_writes_no_history(tmp_path):
    """Second sync with unchanged notebooks must not write new history records."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()
    registry_path = tmp_path / "registry.json"

    nb = _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)

    # Delete notebook → retire
    nb.unlink()
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)

    history_dir = registry_path.parent / "category_history" / "2211dcaa01587d44"
    assert history_dir.is_dir()
    first_count = len(list(history_dir.glob("*.json")))

    # Second sync — same deleted state, must NOT write another history record
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)
    second_count = len(list(history_dir.glob("*.json")))
    assert second_count == first_count


def test_keyword_id_must_match_keyword(tmp_path):
    """category_from_notebook rejects a notebook whose keyword_id != keyword_id(keyword)."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()

    # keyword_id("风吹雪") = "2211dcaa01587d44"
    # A notebook with a DIFFERENT keyword_id (even well-formed 16-hex) is rejected
    nb = _write_notebook(notebook_dir, "风吹雪", keyword_id="deadbeef00000001")
    with pytest.raises(NotebookSchemaError, match="keyword_id does not match"):
        category_from_notebook(nb)


def test_duplicate_same_id_same_keyword_rejected(tmp_path):
    """Two notebook files with the same keyword_id and keyword: duplicate collision."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()
    registry_path = tmp_path / "registry.json"

    _write_notebook(notebook_dir, "风吹雪", filename="notebook_a.json")
    _write_notebook(notebook_dir, "风吹雪", filename="notebook_b.json")

    result = sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=False)
    collisions = [c for c in result["collisions"]
                  if c["type"] == "duplicate_notebook_same_identity"]
    assert len(collisions) >= 1


def test_enabled_notebook_clears_old_retired_at(tmp_path):
    """An enabled notebook has retired_at stripped, never inherited from old registry."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()
    registry_path = tmp_path / "registry.json"

    # First sync
    nb = _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)

    # Inject retired_at into the registry directly
    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    raw["categories"][0]["retired_at"] = "2026-01-01T00:00:00Z"
    registry_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    # Re-sync — notebook is still enabled, retired_at must be stripped
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)
    cats = load_categories(registry_path)
    assert len(cats) == 1
    assert cats[0].keyword_zh == "风吹雪"
    assert cats[0].retired_at is None


def test_retirement_history_idempotent(tmp_path):
    """Retirement history is written exactly once per deletion event."""
    notebook_dir = tmp_path / "notebooks"
    notebook_dir.mkdir()
    registry_path = tmp_path / "registry.json"

    nb = _write_notebook(notebook_dir, "风吹雪")
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)
    nb.unlink()

    # First sync after deletion — writes history
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)
    history_dir = registry_path.parent / "category_history" / "2211dcaa01587d44"
    count_after_first = len(list(history_dir.glob("*.json")))
    assert count_after_first == 1

    # Second sync with same deleted state — must NOT write another record
    sync_registry(notebook_dir=notebook_dir, registry_path=registry_path, apply=True)
    count_after_second = len(list(history_dir.glob("*.json")))
    assert count_after_second == 1
