"""Cross-module consistency: shared predicate, schema, store, audit, migration.

Representative matrix (not field-exhaustive).  Field-level coverage is in
unit tests: test_backfill_state, test_keyword_notebook,
test_discovery_keyword_audit_v3, test_keyword_notebook_v3_migration, etc.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from tests.helpers.relevance_profiles import bind_test_relevance_profile

from src.discovery.backfill_state import (
    BackfillBindError,
    describe_nonpristine_unbound_backfill,
    is_strictly_pristine_unbound_backfill,
    resolve_backfill_generation_binding,
)
from src.discovery.keyword_notebook import (
    KeywordNotebookStore,
    notebook_filename,
    query_identity,
    validate_notebook,
)
from src.discovery.page_journal import request_signature

pytestmark = pytest.mark.contract

KEYWORD = "测试风沙"


def _make_store(tmp_path: Path) -> tuple[KeywordNotebookStore, dict]:
    store = KeywordNotebookStore(tmp_path / "notebooks")
    store.ensure_notebook(KEYWORD)
    store.sync_search_queries(KEYWORD, add=[
        {"query": KEYWORD, "language": "zh", "source": "pytest"},
        {"query": "test paper", "language": "en", "source": "pytest"},
    ])
    bind_test_relevance_profile(store, KEYWORD)
    store.set_enabled(KEYWORD, True)
    return store, store.require_v3(KEYWORD)


def _backfill_from(nb: dict) -> dict:
    qid = query_identity("en", "test paper")
    return nb["search_queries"][qid]["providers"]["openalex"]["backfill"]


def _corrupt(store: KeywordNotebookStore, updates: dict) -> None:
    qid = query_identity("en", "test paper")
    path = store.notebook_dir / notebook_filename(KEYWORD)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["search_queries"][qid]["providers"]["openalex"]["backfill"].update(updates)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ── Representative cases: each case checks all consumers inline ──────


def test_strict_pristine_consistency(tmp_path):
    """Pristine: helper=true, schema=pass, store=first-bind, no recovery ops."""
    store, nb = _make_store(tmp_path)
    bf = _backfill_from(nb)

    # Shared helper
    assert is_strictly_pristine_unbound_backfill(bf) is True
    assert describe_nonpristine_unbound_backfill(bf) == ()

    # Schema
    path = store.notebook_dir / notebook_filename(KEYWORD)
    raw = json.loads(path.read_text(encoding="utf-8"))
    validate_notebook(raw)  # must not raise

    # Store first bind
    qid = query_identity("en", "test paper")
    sig = request_signature(page_size=10)
    result = store.ensure_backfill_generation(KEYWORD, qid, "openalex", request_signature_hash=sig["hash"])
    assert result["request_signature"] == sig["hash"]
    assert result["generation"] >= 1

    # Recovery: no operations
    from scripts.recover_discovery_keyword_notebooks import recover_notebooks
    tmp_path.joinpath("pages").mkdir(exist_ok=True)
    tmp_path.joinpath("locks").mkdir(exist_ok=True)
    recover_store = KeywordNotebookStore(tmp_path / "nb_recover")
    recover_store.ensure_notebook("测试恢复")
    recover_store.sync_search_queries("测试恢复", add=[
        {"query": "测试恢复", "language": "zh", "source": "pytest"},
        {"query": "recovery test", "language": "en", "source": "pytest"},
    ])
    bind_test_relevance_profile(recover_store, "测试恢复")
    recover_store.set_enabled("测试恢复", True)
    report = recover_notebooks(
        notebook_dir=tmp_path / "nb_recover",
        pending_pages_dir=tmp_path / "pages",
        locks_dir=tmp_path / "locks",
    )
    assert report["errors"] == []


def test_durable_progress_fail_closed(tmp_path):
    """Progress (items_returned without signature): all consumers reject."""
    overrides = {"items_returned_total": 1}
    store, nb = _make_store(tmp_path)
    bf = _backfill_from(nb)

    # Shared helper
    bf.update(overrides)
    assert is_strictly_pristine_unbound_backfill(bf) is False

    # Schema
    _corrupt(store, overrides)
    payload = json.loads((store.notebook_dir / notebook_filename(KEYWORD)).read_text(encoding="utf-8"))
    with pytest.raises(Exception):
        validate_notebook(payload)

    # Store = reject, preserve file
    sha_before = hashlib.sha256((store.notebook_dir / notebook_filename(KEYWORD)).read_bytes()).hexdigest()
    sig = request_signature(page_size=10)
    try:
        store.ensure_backfill_generation(KEYWORD, query_identity("en", "test paper"), "openalex",
                                          request_signature_hash=sig["hash"])
    except Exception:
        pass
    sha_after = hashlib.sha256((store.notebook_dir / notebook_filename(KEYWORD)).read_bytes()).hexdigest()
    assert sha_before == sha_after

    # Store decision
    with pytest.raises((BackfillBindError, ValueError)):
        resolve_backfill_generation_binding(bf, "a1b2c3d4e5f6a7b8")


def test_invalid_type_fail_closed(tmp_path):
    """generation=0: all consumers reject."""
    overrides = {"generation": 0}
    store, nb = _make_store(tmp_path)
    bf = _backfill_from(nb)
    bf.update(overrides)
    assert is_strictly_pristine_unbound_backfill(bf) is False

    _corrupt(store, overrides)
    payload = json.loads((store.notebook_dir / notebook_filename(KEYWORD)).read_text(encoding="utf-8"))
    with pytest.raises(Exception):
        validate_notebook(payload)

    with pytest.raises((BackfillBindError, ValueError)):
        resolve_backfill_generation_binding(bf, "a1b2c3d4e5f6a7b8")


def test_terminal_failure_fail_closed(tmp_path):
    """terminal_failure=True: all consumers reject."""
    overrides = {"terminal_failure": True}
    store, nb = _make_store(tmp_path)
    bf = _backfill_from(nb)
    bf.update(overrides)
    assert is_strictly_pristine_unbound_backfill(bf) is False

    _corrupt(store, overrides)
    payload = json.loads((store.notebook_dir / notebook_filename(KEYWORD)).read_text(encoding="utf-8"))
    with pytest.raises(Exception):
        validate_notebook(payload)

    with pytest.raises((BackfillBindError, ValueError)):
        resolve_backfill_generation_binding(bf, "a1b2c3d4e5f6a7b8")


def test_unknown_field_fail_closed(tmp_path):
    """Unknown field: exact-schema reject."""
    overrides = {"some_unknown_field": True}
    store, nb = _make_store(tmp_path)
    bf = _backfill_from(nb)
    bf.update(overrides)
    assert is_strictly_pristine_unbound_backfill(bf) is False

    _corrupt(store, overrides)
    payload = json.loads((store.notebook_dir / notebook_filename(KEYWORD)).read_text(encoding="utf-8"))
    with pytest.raises(Exception):
        validate_notebook(payload)

    with pytest.raises((BackfillBindError, ValueError)):
        resolve_backfill_generation_binding(bf, "a1b2c3d4e5f6a7b8")


# ── Audit-specific contracts ────────────────────────────────────────


def test_audit_strict_pristine_is_summary_only(tmp_path):
    """Audit passes pristine notebooks with only summary counts."""
    from scripts.audit_discovery_keyword_index_sources import run_audit
    from src.catalog_folders.registry import definition_hash
    import scripts.audit_discovery_keyword_index_sources as audit_module
    notebooks = tmp_path / "nb"; pending = tmp_path / "pg"
    reg = tmp_path / "reg"; locks = tmp_path / "locks"
    exports = tmp_path / "exports"; catalog = tmp_path / "cat"
    for p in (notebooks, pending, reg, locks, exports, catalog): p.mkdir()
    audit_module.DISCOVERY_KEYWORD_NOTEBOOK_DIR = notebooks
    audit_module.DISCOVERY_PENDING_PAGES_DIR = pending
    audit_module.DISCOVERY_LOCKS_DIR = locks; audit_module.DISCOVERY_EXPORTS_DIR = exports
    audit_module.DISCOVERY_DIR = tmp_path / "disc"
    audit_module.CATALOG_FOLDER_ROOT = catalog; audit_module.CATALOG_STATE_ROOT = reg
    store = KeywordNotebookStore(notebooks)
    store.ensure_notebook("测试审计"); store.sync_search_queries("测试审计", add=[
        {"query": "测试审计", "language": "zh", "source": "pytest"},
        {"query": "audit test", "language": "en", "source": "pytest"},
    ]); bind_test_relevance_profile(store, "测试审计"); store.set_enabled("测试审计", True)
    nb = store.require_v3("测试审计")
    row = {"category_id": nb["keyword_id"], "keyword_zh": nb["keyword_zh"],
           "normalized_keyword_zh": nb["keyword_zh"], "directory_name": nb["keyword_zh"],
           "source_notebook": notebook_filename(nb["keyword_zh"]),
           "guidance_zh": None, "aliases_zh": [], "exclusions_zh": [],
           "classification_enabled": True}
    row["definition_sha256"] = definition_hash(row)
    (reg / "category_registry.json").write_text(json.dumps(
        {"schema_version": "1.0", "categories": [row]}, ensure_ascii=False), encoding="utf-8")
    (catalog / nb["keyword_zh"]).mkdir(parents=True, exist_ok=True)
    report = run_audit()
    assert report["errors"] == []
    assert report["summary"]["pristine_unbound_lanes"] == 4


def test_audit_page_journal_without_sig_state_only(tmp_path):
    """Page journal on disk with state-only pristine notebook is audit error."""
    from src.discovery.models import PaperCandidate
    from src.discovery.page_journal import PageJournalStore
    from scripts.audit_discovery_keyword_index_sources import run_audit
    from src.catalog_folders.registry import definition_hash
    import scripts.audit_discovery_keyword_index_sources as audit_module
    notebooks = tmp_path / "nb"; pending = tmp_path / "pg"
    reg = tmp_path / "reg"; locks = tmp_path / "locks"
    exports = tmp_path / "exports"; catalog = tmp_path / "cat"
    for p in (notebooks, pending, reg, locks, exports, catalog): p.mkdir()
    audit_module.DISCOVERY_KEYWORD_NOTEBOOK_DIR = notebooks
    audit_module.DISCOVERY_PENDING_PAGES_DIR = pending
    audit_module.DISCOVERY_LOCKS_DIR = locks; audit_module.DISCOVERY_EXPORTS_DIR = exports
    audit_module.DISCOVERY_DIR = tmp_path / "disc"
    audit_module.CATALOG_FOLDER_ROOT = catalog; audit_module.CATALOG_STATE_ROOT = reg
    store = KeywordNotebookStore(notebooks)
    store.ensure_notebook("测试审计"); store.sync_search_queries("测试审计", add=[
        {"query": "测试审计", "language": "zh", "source": "pytest"},
        {"query": "audit test", "language": "en", "source": "pytest"},
    ]); bind_test_relevance_profile(store, "测试审计"); store.set_enabled("测试审计", True)
    nb = store.require_v3("测试审计")
    row = {"category_id": nb["keyword_id"], "keyword_zh": nb["keyword_zh"],
           "normalized_keyword_zh": nb["keyword_zh"], "directory_name": nb["keyword_zh"],
           "source_notebook": notebook_filename(nb["keyword_zh"]),
           "guidance_zh": None, "aliases_zh": [], "exclusions_zh": [],
           "classification_enabled": True}
    row["definition_sha256"] = definition_hash(row)
    (reg / "category_registry.json").write_text(json.dumps(
        {"schema_version": "1.0", "categories": [row]}, ensure_ascii=False), encoding="utf-8")
    (catalog / nb["keyword_zh"]).mkdir(parents=True, exist_ok=True)
    qid = query_identity("en", "audit test")
    sig = request_signature(page_size=10)
    journal = PageJournalStore(pending)
    page = journal.make_page(page_id="p1", keyword_id=nb["keyword_id"],
        keyword_zh="测试审计", query_id=qid, query="audit test",
        query_language="en", provider="openalex", lane="backfill", generation=1,
        request_signature_value=sig, request_cursor=None,
        next_cursor="opaque-next", provider_exhausted=False,
        state="cursor_committed",
        candidates=[PaperCandidate(title="T", doi="10.1234/a")])
    p = journal.page_path(keyword_id=nb["keyword_id"], query_id=qid,
        provider="openalex", lane="backfill", page_id="p1")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
    report = run_audit()
    assert any(row["kind"] == "generation" for row in report["errors"])
    assert report["backfill_state_safe"] is False


# ── Migration-specific contracts ────────────────────────────────────


def test_v3_source_not_passed_to_legacy_normalizer(tmp_path):
    """v3 source is NOT processed by the legacy normalizer."""
    from src.discovery.notebook_v3_migration import _legacy_queries, _translate_legacy_provider_state as legacy_fn
    import src.discovery.notebook_v3_migration as mig
    import unittest.mock as mock
    store = KeywordNotebookStore(tmp_path / "nb")
    store.ensure_notebook("测试迁移"); store.sync_search_queries("测试迁移", add=[
        {"query": "测试迁移", "language": "zh", "source": "pytest"},
        {"query": "migrate test", "language": "en", "source": "pytest"},
    ]); bind_test_relevance_profile(store, "测试迁移"); store.set_enabled("测试迁移", True)
    nb = store.require_v3("测试迁移")
    with mock.patch.object(mig, "_translate_legacy_provider_state", wraps=legacy_fn) as spy:
        queries = _legacy_queries(nb, filename="test_v3.json", migration_at="2026-01-01T00:00:00Z")
        spy.assert_not_called()
        assert len(queries) == 2


def test_migration_blocked_exception(tmp_path):
    """Non-pristine unbound raises typed MigrationBlocked exception."""
    from src.discovery.notebook_v3_migration import MigrationBlocked, _legacy_queries
    store = KeywordNotebookStore(tmp_path / "nb")
    store.ensure_notebook("测试迁移"); store.sync_search_queries("测试迁移", add=[
        {"query": "测试迁移", "language": "zh", "source": "pytest"},
        {"query": "migrate test", "language": "en", "source": "pytest"},
    ]); bind_test_relevance_profile(store, "测试迁移"); store.set_enabled("测试迁移", True)
    nb = store.require_v3("测试迁移")
    qid = query_identity("en", "migrate test")
    nb["search_queries"][qid]["providers"]["openalex"]["backfill"]["items_returned_total"] = 1
    with pytest.raises(MigrationBlocked):
        _legacy_queries(nb, filename="corrupt.json", migration_at="2026-01-01T00:00:00Z")
