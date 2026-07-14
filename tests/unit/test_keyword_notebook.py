"""Unit tests for src/discovery/keyword_notebook.py — v3 schema only."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.discovery.keyword_notebook import (
    INITIAL_CURSOR,
    SCHEMA_VERSION,
    DiscoveryNotReadyError,
    DiscoveryReadiness,
    KeywordNotebookStore,
    LegacyNotebookSchemaError,
    NotebookCorruptError,
    UnsupportedNotebookSchemaError,
    composite_backfill_signature,
    detect_query_language,
    keyword_fingerprint8,
    keyword_id,
    notebook_filename,
    normalize_keyword,
    pagination_signature,
    query_identity,
    validate_notebook,
    validate_discovery_readiness,
)
from tests.fixtures.legacy.notebook_v2 import (
    RETIRED_QUERY_CONTAINER_FIELD,
    inject_retired_query_container,
    v2_notebook_payload,
)


pytestmark = pytest.mark.unit


def _query_id(query: str) -> str:
    language = detect_query_language(query)
    assert language in {"zh", "en"}
    return query_identity(language, normalize_keyword(query))


def _seed_queries(
    store: KeywordNotebookStore,
    keyword_zh: str,
    queries: list[str],
    signature: str = "",
) -> None:
    store.ensure_notebook(keyword_zh)
    store.sync_search_queries(
        keyword_zh,
        add=[{"query": query, "language": detect_query_language(query)} for query in queries],
        pag_sig=signature,
    )


# ── normalization & identity ─────────────────────────────────────────


class TestNormalizeKeyword:
    def test_strips_and_folds_whitespace(self):
        assert normalize_keyword("  atmospheric   boundary  layer  ") == "atmospheric boundary layer"

    def test_nfc_normalization(self):
        composed = "é"
        decomposed = "é"
        assert normalize_keyword(decomposed) == composed

    def test_empty(self):
        assert normalize_keyword("") == ""


class TestIdentity:
    def test_case_and_whitespace_fold_to_same_id(self):
        assert keyword_id("Atmospheric Boundary Layer") == keyword_id(" atmospheric  boundary layer ")
        assert keyword_id("Atmospheric Boundary Layer") == keyword_id("ATMOSPHERIC BOUNDARY LAYER")

    def test_different_keywords_different_id(self):
        assert keyword_id("snow physics") != keyword_id("sand physics")

    def test_fingerprint_is_id_prefix(self):
        kid = keyword_id("boundary layer")
        assert kid.startswith(keyword_fingerprint8("boundary layer"))

    def test_filename_contains_slug_and_fp(self):
        fn = notebook_filename("Atmospheric Boundary Layer")
        assert "__" in fn
        assert fn.endswith(".json")
        assert keyword_fingerprint8("Atmospheric Boundary Layer") in fn


# ── language detection ───────────────────────────────────────────────


class TestLanguageDetection:
    def test_chinese_query_is_zh(self):
        assert detect_query_language("大气边界层") == "zh"
        assert detect_query_language("风吹雪") == "zh"

    def test_english_query_is_en(self):
        assert detect_query_language("blowing snow") == "en"
        assert detect_query_language("atmospheric boundary layer") == "en"

    def test_mixed_query_is_mixed(self):
        assert detect_query_language("atmospheric 大气边界层") == "mixed"

    def test_empty_query_is_invalid(self):
        assert detect_query_language("") == "invalid"

    def test_numeric_only_query_is_invalid(self):
        assert detect_query_language("123 456") == "invalid"

    def test_punctuation_only_query_is_invalid(self):
        assert detect_query_language("+ - *") == "invalid"

    def test_english_query_requires_latin_letter(self):
        # "100-year" has letters, so it's English
        assert detect_query_language("100-year flood") == "en"

    def test_chinese_query_requires_cjk(self):
        assert detect_query_language("风沙动力学 123") == "zh"


class TestQueryIdentity:
    def test_same_language_and_query_same_identity(self):
        a = query_identity("zh", normalize_keyword("风吹雪"))
        b = query_identity("zh", normalize_keyword("风吹雪"))
        assert a == b

    def test_different_language_different_identity(self):
        a = query_identity("zh", normalize_keyword("风吹雪"))
        b = query_identity("en", normalize_keyword("风吹雪"))
        assert a != b

    def test_identity_length_is_16_hex(self):
        qid = query_identity("en", "blowing snow")
        assert len(qid) == 16
        assert all(c in "0123456789abcdef" for c in qid)

    def test_english_identity_is_casefolded(self):
        assert query_identity("en", "Blowing Snow") == query_identity("en", "blowing snow")


# ── discovery readiness ──────────────────────────────────────────────


class TestDiscoveryReadiness:
    def test_ready_notebook_passes(self):
        nb = {
            "schema_version": "3.0",
            "keyword_id": keyword_id("风吹雪"),
            "keyword_zh": "风吹雪",
            "search_queries": {
                "q1": {"query": "风吹雪", "language": "zh", "active": True},
                "q2": {"query": "blowing snow", "language": "en", "active": True},
            },
        }
        r = validate_discovery_readiness(nb)
        assert r.ready is True
        assert r.zh_count == 1
        assert r.en_count == 1

    def test_missing_keyword_zh_fails(self):
        nb = {"schema_version": "3.0", "keyword_zh": "", "search_queries": {}}
        r = validate_discovery_readiness(nb)
        assert r.ready is False

    def test_missing_search_queries_fails(self):
        nb = {"schema_version": "3.0", "keyword_zh": "风吹雪"}
        r = validate_discovery_readiness(nb)
        assert r.ready is False

    def test_no_active_zh_fails(self):
        nb = {
            "schema_version": "3.0", "keyword_zh": "风吹雪",
            "keyword_id": keyword_id("风吹雪"),
            "search_queries": {
                "q1": {"query": "blowing snow", "language": "en", "active": True},
            },
        }
        r = validate_discovery_readiness(nb)
        assert r.ready is False
        assert "no active Chinese" in str(r.errors)

    def test_no_active_en_fails(self):
        nb = {
            "schema_version": "3.0", "keyword_zh": "风吹雪",
            "keyword_id": keyword_id("风吹雪"),
            "search_queries": {
                "q1": {"query": "风吹雪", "language": "zh", "active": True},
            },
        }
        r = validate_discovery_readiness(nb)
        assert r.ready is False
        assert "no active English" in str(r.errors)

    def test_chinese_synonym_does_not_replace_canonical_keyword(self):
        nb = {
            "schema_version": "3.0", "keyword_zh": "风吹雪",
            "keyword_id": keyword_id("风吹雪"),
            "search_queries": {
                "q1": {"query": "暴风雪", "language": "zh", "active": True},
                "q2": {"query": "blowing snow", "language": "en", "active": True},
            },
        }
        r = validate_discovery_readiness(nb)
        assert r.ready is False
        assert "exactly matches keyword_zh" in str(r.errors)

    def test_invalid_query_rejected(self):
        nb = {
            "schema_version": "3.0", "keyword_zh": "风吹雪",
            "keyword_id": keyword_id("风吹雪"),
            "search_queries": {
                "q1": {"query": "风吹雪", "language": "zh", "active": True},
                "q2": {"query": "123", "language": "en", "active": True},
            },
        }
        r = validate_discovery_readiness(nb)
        assert r.ready is False

    def test_declared_language_mismatch_fails(self):
        nb = {
            "schema_version": "3.0", "keyword_zh": "风吹雪",
            "keyword_id": keyword_id("风吹雪"),
            "search_queries": {
                "q1": {"query": "风吹雪", "language": "zh", "active": True},
                "q2": {"query": "blowing snow", "language": "zh", "active": True},
            },
        }
        r = validate_discovery_readiness(nb)
        assert r.ready is False


# ── v3 notebook CRUD ─────────────────────────────────────────────────


class TestV3Lifecycle:
    def test_ensure_notebook_creates_v3(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        nb = store.ensure_notebook("风吹雪")
        assert nb["schema_version"] == SCHEMA_VERSION
        assert nb["keyword_zh"] == "风吹雪"
        assert "search_queries" in nb
        assert "keyword" not in nb  # no legacy field
        assert len(nb["search_queries"]) == 0

    def test_ensure_notebook_does_not_touch_existing_queries(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        store.ensure_notebook("风吹雪")
        store.sync_search_queries("风吹雪", add=[{"query": "风吹雪", "language": "zh"}])
        nb = store.ensure_notebook("风吹雪")
        assert len(nb["search_queries"]) == 1

    def test_sync_search_queries_adds_bilingual(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        store.ensure_notebook("风吹雪")
        store.sync_search_queries("风吹雪", add=[
            {"query": "风吹雪", "language": "zh", "source": "canonical"},
            {"query": "blowing snow", "language": "en", "source": "curated"},
        ])
        queries = store.active_search_queries("风吹雪")
        assert len(queries) == 2
        langs = {q["language"] for q in queries}
        assert "zh" in langs
        assert "en" in langs

    def test_sync_search_queries_disables_query(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        store.ensure_notebook("风吹雪")
        store.sync_search_queries("风吹雪", add=[
            {"query": "blowing snow", "language": "en"},
            {"query": "drifting snow", "language": "en"},
        ])
        store.sync_search_queries("风吹雪", disable=["drifting snow"])
        queries = store.active_search_queries("风吹雪")
        assert len(queries) == 1
        assert queries[0]["query"] == "blowing snow"

    def test_sync_search_queries_enables_query(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        store.ensure_notebook("风吹雪")
        store.sync_search_queries("风吹雪", add=[{"query": "blowing snow", "language": "en"}])
        store.sync_search_queries("风吹雪", disable=["blowing snow"])
        assert len(store.active_search_queries("风吹雪")) == 0
        store.sync_search_queries("风吹雪", enable=["blowing snow"])
        assert len(store.active_search_queries("风吹雪")) == 1

    def test_sync_search_queries_idempotent(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        store.ensure_notebook("风吹雪")
        store.sync_search_queries("风吹雪", add=[{"query": "风吹雪", "language": "zh"}])
        store.sync_search_queries("风吹雪", add=[{"query": "风吹雪", "language": "zh"}])
        queries = store.active_search_queries("风吹雪")
        assert len(queries) == 1

    def test_sync_search_queries_rejects_invalid(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        store.ensure_notebook("风吹雪")
        with pytest.raises(ValueError, match="valid text query"):
            store.sync_search_queries("风吹雪", add=[{"query": "123", "language": "en"}])
        assert len(store.active_search_queries("风吹雪")) == 0

    def test_sync_batch_is_atomic_and_unknown_toggle_fails(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        store.ensure_notebook("风吹雪")
        with pytest.raises(ValueError):
            store.sync_search_queries("风吹雪", add=[
                {"query": "风吹雪", "language": "zh"},
                {"query": "english 风吹雪", "language": "en"},
            ])
        assert store.require_v3("风吹雪")["search_queries"] == {}
        with pytest.raises(ValueError, match="unknown disable"):
            store.sync_search_queries("风吹雪", disable=["blowing snow"])

    def test_case_variants_share_one_query_and_history_is_recorded(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        store.ensure_notebook("风吹雪")
        store.sync_search_queries("风吹雪", add=[
            {"query": "Blowing Snow", "language": "en"},
            {"query": "blowing snow", "language": "en"},
        ], reason="curation", operator="tester")
        nb = store.require_v3("风吹雪")
        assert len(nb["search_queries"]) == 1
        assert nb["definition_history"][-1]["reason"] == "curation"

    def test_mixed_query_has_stable_identity_and_is_schema_valid(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        store.ensure_notebook("风吹雪")
        store.sync_search_queries("风吹雪", add=[
            {"query": "风吹雪 blowing snow", "language": "mixed"},
        ])
        nb = store.require_v3("风吹雪")
        row = next(iter(nb["search_queries"].values()))
        assert row["language"] == "mixed"

    def test_active_search_queries_returns_language_and_source(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        store.ensure_notebook("风吹雪")
        store.sync_search_queries("风吹雪", add=[
            {"query": "风吹雪", "language": "zh", "source": "canonical"},
            {"query": "blowing snow", "language": "en"},
        ])
        queries = store.active_search_queries("风吹雪")
        zh = [q for q in queries if q["language"] == "zh"][0]
        assert zh["source"] == "canonical"
        en = [q for q in queries if q["language"] == "en"][0]
        assert en["source"] == "curated"

    def test_require_v3_ready_passes_with_bilingual(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        store.ensure_notebook("风吹雪")
        store.sync_search_queries("风吹雪", add=[
            {"query": "风吹雪", "language": "zh"},
            {"query": "blowing snow", "language": "en"},
        ])
        store.set_enabled("风吹雪", True)
        nb = store.require_v3_ready("风吹雪")
        assert nb["keyword_zh"] == "风吹雪"

    def test_require_v3_ready_fails_without_english(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        store.ensure_notebook("风吹雪")
        store.sync_search_queries("风吹雪", add=[{"query": "风吹雪", "language": "zh"}])
        with pytest.raises(DiscoveryNotReadyError, match="no active English"):
            store.set_enabled("风吹雪", True)
        assert store.show("风吹雪")["enabled"] is False

    def test_disabled_notebook_not_ready(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        store.ensure_notebook("风吹雪")
        store.set_enabled("风吹雪", False)
        with pytest.raises(DiscoveryNotReadyError, match="disabled"):
            store.require_v3_ready("风吹雪")

    def test_enabled_invalid_notebook_mutation_does_not_write(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        store.ensure_notebook("风吹雪")
        store.sync_search_queries("风吹雪", add=[
            {"query": "风吹雪", "language": "zh"},
            {"query": "blowing snow", "language": "en"},
        ])
        store.set_enabled("风吹雪", True)
        path = tmp_path / notebook_filename("风吹雪")
        payload = json.loads(path.read_text(encoding="utf-8"))
        for entry in payload["search_queries"].values():
            if entry["language"] == "en":
                entry["active"] = False
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        before = path.read_bytes()
        with pytest.raises(DiscoveryNotReadyError, match="not ready"):
            store.sync_search_queries(
                "风吹雪", add=[{"query": "边界层湍流", "language": "zh"}],
            )
        assert path.read_bytes() == before

    @pytest.mark.parametrize("mutation", [
        lambda nb: nb.pop("enabled"),
        lambda nb: nb.__setitem__("keyword_id", "0" * 16),
        lambda nb: nb.__setitem__("normalized_keyword_zh", "wrong"),
        inject_retired_query_container,
    ])
    def test_public_validator_rejects_malformed_v3(self, tmp_path: Path, mutation):
        store = KeywordNotebookStore(tmp_path)
        store.ensure_notebook("风吹雪")
        nb = json.loads(json.dumps(store.require_v3("风吹雪")))
        mutation(nb)
        with pytest.raises(NotebookCorruptError):
            validate_notebook(nb)


# ── cursor operations ────────────────────────────────────────────────


class TestCursorOps:
    """Cursor operations use stable query ids independent of pagination."""

    def test_different_keywords_independent_cursors(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        _seed_queries(store, "主题甲", ["query A"], sig)
        _seed_queries(store, "主题乙", ["query B"], sig)
        query_a = _query_id("query A")
        query_b = _query_id("query B")
        store.advance_backfill("主题甲", query_a, "openalex", next_cursor="A2", items_this_page=10)
        assert store.get_backfill_cursor("主题甲", query_a, "openalex") == "A2"
        assert store.get_backfill_cursor("主题乙", query_b, "openalex") == INITIAL_CURSOR

    def test_new_query_does_not_reset_existing(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        _seed_queries(store, "测试主题", ["query A", "query B"], sig)
        query_a = _query_id("query A")
        store.advance_backfill("测试主题", query_a, "openalex", next_cursor="A99", items_this_page=5)
        _seed_queries(store, "测试主题", ["query C"], sig)
        assert store.get_backfill_cursor("测试主题", query_a, "openalex") == "A99"
        query_c = _query_id("query C")
        assert store.get_backfill_cursor("测试主题", query_c, "openalex") == INITIAL_CURSOR

    def test_inactive_query_preserved_not_deleted(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        _seed_queries(store, "测试主题", ["query A", "query B"], sig)
        query_b = _query_id("query B")
        store.advance_backfill("测试主题", query_b, "crossref", next_cursor="B5", items_this_page=3)
        store.sync_search_queries("测试主题", disable=["query B"])
        nb = store.require_v3("测试主题")
        assert query_b in nb["search_queries"]
        assert nb["search_queries"][query_b]["active"] is False
        assert nb["search_queries"][query_b]["providers"]["crossref"]["backfill"]["cursor"] == "B5"

    def test_refresh_update_does_not_overwrite_backfill_cursor(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        _seed_queries(store, "测试主题", ["query A"], sig)
        query_id_value = _query_id("query A")
        store.advance_backfill("测试主题", query_id_value, "openalex", next_cursor="BACKFILL42", items_this_page=8)
        store.complete_refresh("测试主题", query_id_value, "openalex", status="success", pages_scanned=2, items_returned=100)
        assert store.get_backfill_cursor("测试主题", query_id_value, "openalex") == "BACKFILL42"

    def test_backfill_update_does_not_overwrite_refresh_stats(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        _seed_queries(store, "测试主题", ["query A"], sig)
        query_id_value = _query_id("query A")
        store.complete_refresh("测试主题", query_id_value, "openalex", status="success", pages_scanned=3, items_returned=150)
        store.advance_backfill("测试主题", query_id_value, "openalex", next_cursor="X1", items_this_page=5)
        nb = store.require_v3("测试主题")
        r = nb["search_queries"][query_id_value]["providers"]["openalex"]["refresh"]
        assert r["pages_scanned_last_run"] == 3
        assert r["items_returned_last_run"] == 150
        assert r["last_status"] == "success"

    def test_backfill_failure_does_not_advance_cursor(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        _seed_queries(store, "测试主题", ["query A"], sig)
        query_id_value = _query_id("query A")
        store.advance_backfill("测试主题", query_id_value, "openalex", next_cursor="C3", items_this_page=5)
        store.record_backfill_error("测试主题", query_id_value, "openalex", error="timeout")
        assert store.get_backfill_cursor("测试主题", query_id_value, "openalex") == "C3"
        nb = store.require_v3("测试主题")
        assert nb["search_queries"][query_id_value]["providers"]["openalex"]["backfill"]["last_error"] == "timeout"

    def test_exhausted_flag_set(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        _seed_queries(store, "测试主题", ["query A"], sig)
        query_id_value = _query_id("query A")
        store.advance_backfill("测试主题", query_id_value, "openalex", next_cursor=None, items_this_page=0, exhausted=True)
        assert store.is_backfill_exhausted("测试主题", query_id_value, "openalex") is True

    def test_corrupt_json_fails_closed(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        store.ensure_notebook("测试主题")
        store.sync_search_queries("测试主题", add=[{"query": "query A", "language": "en"}])
        path = tmp_path / notebook_filename("测试主题")
        path.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(NotebookCorruptError):
            store.load("测试主题")

    def test_legacy_v2_notebook_rejected(self, tmp_path: Path):
        """Active code must reject v2 notebooks."""
        kw = "v2test"
        kid = keyword_id(kw)
        path = tmp_path / notebook_filename(kw)
        payload = v2_notebook_payload(kid)
        path.write_text(json.dumps(payload), encoding="utf-8")
        store = KeywordNotebookStore(tmp_path)
        with pytest.raises(LegacyNotebookSchemaError):
            store.load(kw)

    def test_unknown_schema_rejected(self, tmp_path: Path):
        kw = "badschema"
        kid = keyword_id(kw)
        path = tmp_path / notebook_filename(kw)
        payload = {"schema_version": "99.0", "keyword_id": kid}
        path.write_text(json.dumps(payload), encoding="utf-8")
        store = KeywordNotebookStore(tmp_path)
        with pytest.raises(UnsupportedNotebookSchemaError):
            store.load(kw)


# ── keyword_id stability ─────────────────────────────────────────────


class TestKeywordIdStability:
    def test_keyword_id_derived_only_from_keyword_zh(self):
        assert keyword_id("风吹雪") == keyword_id("风吹雪")
        assert keyword_id("风吹雪") != keyword_id("blowing snow")

    def test_adding_english_query_does_not_change_keyword_id(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        store.ensure_notebook("风吹雪")
        kid_before = store.require_v3("风吹雪")["keyword_id"]
        store.sync_search_queries("风吹雪", add=[{"query": "blowing snow", "language": "en"}])
        kid_after = store.require_v3("风吹雪")["keyword_id"]
        assert kid_before == kid_after


# ── reset ────────────────────────────────────────────────────────────


class TestReset:
    def test_reset_backfill_only(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        _seed_queries(store, "测试主题", ["query A"], sig)
        query_id_value = _query_id("query A")
        store.advance_backfill("测试主题", query_id_value, "openalex", next_cursor="Z9", items_this_page=5)
        store.advance_backfill("测试主题", query_id_value, "crossref", next_cursor="Y8", items_this_page=5)
        store.reset_backfill("测试主题", reason="test", pag_sig=sig)
        assert store.get_backfill_cursor("测试主题", query_id_value, "openalex") == INITIAL_CURSOR
        assert store.get_backfill_cursor("测试主题", query_id_value, "crossref") == INITIAL_CURSOR
        nb = store.require_v3("测试主题")
        assert len(nb.get("reset_history", [])) == 1

    def test_reset_does_not_affect_other_keywords(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        _seed_queries(store, "主题甲", ["query A"], sig)
        _seed_queries(store, "主题乙", ["query B"], sig)
        query_a = _query_id("query A")
        query_b = _query_id("query B")
        store.advance_backfill("主题甲", query_a, "openalex", next_cursor="A5", items_this_page=5)
        store.advance_backfill("主题乙", query_b, "openalex", next_cursor="B5", items_this_page=5)
        store.reset_backfill("主题甲", reason="test", pag_sig=sig)
        assert store.get_backfill_cursor("主题甲", query_a, "openalex") == INITIAL_CURSOR
        assert store.get_backfill_cursor("主题乙", query_b, "openalex") == "B5"

    def test_reset_does_not_delete_notebook(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        _seed_queries(store, "测试主题", ["query A"], sig)
        store.reset_backfill("测试主题", reason="test", pag_sig=sig)
        assert store.require_v3("测试主题") is not None


# ── pagination signature ─────────────────────────────────────────────


class TestPaginationSignature:
    def test_sort_change_creates_new_signature(self):
        assert pagination_signature(sort="relevance") != pagination_signature(sort="date")

    def test_same_params_same_signature(self):
        assert pagination_signature(sort="relevance") == pagination_signature(sort="relevance")

    def test_changed_signature_resets_backfill(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig1 = pagination_signature(sort="relevance")
        _seed_queries(store, "测试主题", ["query A"], sig1)
        query_id_value = _query_id("query A")
        store.advance_backfill("测试主题", query_id_value, "openalex", next_cursor="CURSOR1", items_this_page=5)
        sig2 = pagination_signature(sort="date")
        state = store.ensure_backfill_generation(
            "测试主题", query_id_value, "openalex", request_signature_hash=sig2,
        )
        assert store.get_backfill_cursor("测试主题", query_id_value, "openalex") == INITIAL_CURSOR
        assert state["generation"] == 2
        assert state["generation_history"][0]["cursor"] == "CURSOR1"

    def test_composite_backfill_signature_ignores_refresh_sort(self):
        sig1 = composite_backfill_signature(page_size=50, openalex_backfill_sort="cited_by_count:desc", crossref_backfill_sort="published")
        sig2 = composite_backfill_signature(page_size=50, openalex_backfill_sort="cited_by_count:desc", crossref_backfill_sort="published")
        sig3 = composite_backfill_signature(page_size=50, openalex_backfill_sort="publication_date:desc", crossref_backfill_sort="published")
        assert sig1 == sig2
        assert sig1 != sig3


class TestBackpressure:
    def test_backpressure_uses_hysteresis(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        _seed_queries(store, "测试主题", ["query A"], pagination_signature())
        state = store.update_backpressure("测试主题", pending_count=1000, max_threshold=1000, resume_threshold=700)
        assert state["active"] is True
        state = store.update_backpressure("测试主题", pending_count=999, max_threshold=1000, resume_threshold=700)
        assert state["active"] is True
        state = store.update_backpressure("测试主题", pending_count=700, max_threshold=1000, resume_threshold=700)
        assert state["active"] is False

    def test_backpressure_rejects_bad_thresholds(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        _seed_queries(store, "测试主题", ["query A"], pagination_signature())
        with pytest.raises(ValueError):
            store.update_backpressure("测试主题", pending_count=1, max_threshold=1000, resume_threshold=1000)


# ── list / show ──────────────────────────────────────────────────────


class TestListShow:
    def test_list_returns_all_keywords(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        store.ensure_notebook("主题甲")
        store.ensure_notebook("主题乙")
        items = store.list_keywords()
        assert len(items) == 2
        keywords = {i["keyword_zh"] for i in items}
        assert keywords == {"主题甲", "主题乙"}

    def test_show_missing_returns_none(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        assert store.show("nope") is None

    def test_enable_disable(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        store.ensure_notebook("测试主题")
        store.set_enabled("测试主题", False)
        assert store.show("测试主题")["enabled"] is False
        store.sync_search_queries("测试主题", add=[
            {"query": "测试主题", "language": "zh"},
            {"query": "test topic", "language": "en"},
        ])
        store.set_enabled("测试主题", True)
        assert store.show("测试主题")["enabled"] is True

    def test_show_v3_has_keyword_zh(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        store.ensure_notebook("风吹雪")
        summary = store.show("风吹雪")
        assert summary["keyword_zh"] == "风吹雪"
        assert set(summary) == {
            "keyword_zh", "keyword_id", "enabled", "ready",
            "active_queries", "queries",
        }
        assert summary["ready"] is False


# ── v3 notebook does not contain legacy fields ───────────────────────


class TestV3NoLegacyFields:
    def test_empty_notebook_has_no_keyword_field(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        nb = store.ensure_notebook("风吹雪")
        assert "keyword" not in nb

    def test_empty_notebook_has_no_retired_query_container(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        nb = store.ensure_notebook("风吹雪")
        assert RETIRED_QUERY_CONTAINER_FIELD not in nb
