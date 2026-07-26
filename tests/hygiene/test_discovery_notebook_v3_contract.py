from __future__ import annotations

import ast
from pathlib import Path

import pytest


pytestmark = pytest.mark.hygiene
ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_active_cli_selects_notebooks_not_free_provider_queries():
    for relative in [
        "scripts/discover_papers.py",
        "scripts/discover_papers_concurrent.py",
    ]:
        text = _source(relative)
        assert '"--keyword-zh"' in text, relative
        assert '"--dry-run"' in text, relative
        for legacy_option in ('"--query"', '"--queries-file"', '"--limit-per-query"', '"--reset-keyword-progress"'):
            assert legacy_option not in text, f"{relative}: {legacy_option}"


def test_discovery_cli_has_no_retired_topic_or_batch_query_switches():
    assert "--topic" not in _source("scripts/discover_papers.py")
    manage = _source("scripts/manage_discovery_keywords.py")
    assert "--add-queries" not in manage
    assert '"--add-query-zh"' in manage
    assert '"--add-query-en"' in manage


def test_active_page_pipeline_has_no_legacy_expansion_identity():
    for relative in [
        "src/discovery/coordinator.py",
        "src/discovery/backfill_transaction.py",
    ]:
        text = _source(relative)
        for legacy_name in ("expansion_id", "expanded_query", "expansion_key"):
            assert legacy_name not in text, f"{relative}: {legacy_name}"


def test_catalog_registry_never_reads_search_queries_for_categories():
    relative = "src/catalog_folders/registry.py"
    tree = ast.parse(_source(relative), filename=relative)
    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        key = node.slice
        if isinstance(key, ast.Constant) and key.value == "search_queries":
            offenders.append(node.lineno)
    assert offenders == [], f"Catalog registry reads search_queries at lines {offenders}"


def test_definition_hash_documents_query_exclusion():
    text = _source("src/catalog_folders/identity.py")
    assert "Chinese and English search queries" in text
    assert 'keys = ("category_id", "keyword_zh", "guidance_zh", "aliases_zh", "exclusions_zh")' in text
