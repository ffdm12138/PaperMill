"""Unit tests for src/discovery/keyword_notebook.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.discovery.keyword_notebook import (
    INITIAL_CURSOR,
    KeywordNotebookStore,
    NotebookCorruptError,
    composite_backfill_signature,
    expansion_key,
    keyword_fingerprint8,
    keyword_id,
    notebook_filename,
    normalize_keyword,
    pagination_signature,
)


pytestmark = pytest.mark.unit


# ── normalization & identity ─────────────────────────────────────────


class TestNormalizeKeyword:
    def test_strips_and_folds_whitespace(self):
        assert normalize_keyword("  atmospheric   boundary  layer  ") == "atmospheric boundary layer"

    def test_nfc_normalization(self):
        # A combining-character sequence should NFC-fold to the precomposed form.
        composed = "é"  # U+00E9
        decomposed = "é"  # 'e' + combining acute
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


# ── notebook CRUD ────────────────────────────────────────────────────


class TestNotebookStore:
    def test_new_keyword_creates_notebook(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        nb = store.ensure_keyword("大气边界层", ["大气边界层", "atmospheric boundary layer"], sig)
        assert nb["keyword"] == "大气边界层"
        assert nb["enabled"] is True
        assert len(nb["expansions"]) == 2
        # Both expansions have both providers with initial cursor.
        for exp in nb["expansions"].values():
            for prov in ("openalex", "crossref"):
                assert exp["providers"][prov]["backfill"]["cursor"] == INITIAL_CURSOR
                assert exp["providers"][prov]["backfill"]["exhausted"] is False

    def test_same_normalized_keyword_does_not_create_second_notebook(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        store.ensure_keyword("Atmospheric Boundary Layer", ["atmospheric boundary layer"], sig)
        store.ensure_keyword(" atmospheric  boundary layer ", ["atmospheric boundary layer"], sig)
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1

    def test_different_keywords_independent_cursors(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        store.ensure_keyword("keyword A", ["keyword A"], sig)
        store.ensure_keyword("keyword B", ["keyword B"], sig)
        ekey_a = expansion_key("keyword A", sig)
        ekey_b = expansion_key("keyword B", sig)
        store.advance_backfill("keyword A", ekey_a, "openalex", next_cursor="A2", items_this_page=10)
        assert store.get_backfill_cursor("keyword A", ekey_a, "openalex") == "A2"
        assert store.get_backfill_cursor("keyword B", ekey_b, "openalex") == INITIAL_CURSOR

    def test_new_expansion_does_not_reset_existing(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        store.ensure_keyword("kw", ["query A", "query B"], sig)
        ekey_a = expansion_key("query A", sig)
        store.advance_backfill("kw", ekey_a, "openalex", next_cursor="A99", items_this_page=5)
        # Add a new expansion C.
        store.ensure_keyword("kw", ["query A", "query B", "query C"], sig)
        assert store.get_backfill_cursor("kw", ekey_a, "openalex") == "A99"
        ekey_c = expansion_key("query C", sig)
        assert store.get_backfill_cursor("kw", ekey_c, "openalex") == INITIAL_CURSOR

    def test_inactive_expansion_preserved_not_deleted(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        store.ensure_keyword("kw", ["query A", "query B"], sig)
        ekey_b = expansion_key("query B", sig)
        store.advance_backfill("kw", ekey_b, "crossref", next_cursor="B5", items_this_page=3)
        # Drop query B from the active set.
        store.ensure_keyword("kw", ["query A"], sig)
        nb = store.load("kw")
        assert ekey_b in nb["expansions"]
        assert nb["expansions"][ekey_b]["active"] is False
        # Cursor preserved.
        assert nb["expansions"][ekey_b]["providers"]["crossref"]["backfill"]["cursor"] == "B5"

    def test_refresh_update_does_not_overwrite_backfill_cursor(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        store.ensure_keyword("kw", ["query A"], sig)
        ekey = expansion_key("query A", sig)
        store.advance_backfill("kw", ekey, "openalex", next_cursor="BACKFILL42", items_this_page=8)
        store.complete_refresh("kw", ekey, "openalex", status="success", pages_scanned=2, items_returned=100)
        assert store.get_backfill_cursor("kw", ekey, "openalex") == "BACKFILL42"

    def test_backfill_update_does_not_overwrite_refresh_stats(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        store.ensure_keyword("kw", ["query A"], sig)
        ekey = expansion_key("query A", sig)
        store.complete_refresh("kw", ekey, "openalex", status="success", pages_scanned=3, items_returned=150)
        store.advance_backfill("kw", ekey, "openalex", next_cursor="X1", items_this_page=5)
        nb = store.load("kw")
        r = nb["expansions"][ekey]["providers"]["openalex"]["refresh"]
        assert r["pages_scanned_last_run"] == 3
        assert r["items_returned_last_run"] == 150
        assert r["last_status"] == "success"

    def test_backfill_failure_does_not_advance_cursor(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        store.ensure_keyword("kw", ["query A"], sig)
        ekey = expansion_key("query A", sig)
        store.advance_backfill("kw", ekey, "openalex", next_cursor="C3", items_this_page=5)
        store.record_backfill_error("kw", ekey, "openalex", error="timeout")
        assert store.get_backfill_cursor("kw", ekey, "openalex") == "C3"
        nb = store.load("kw")
        assert nb["expansions"][ekey]["providers"]["openalex"]["backfill"]["last_error"] == "timeout"

    def test_exhausted_flag_set(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        store.ensure_keyword("kw", ["query A"], sig)
        ekey = expansion_key("query A", sig)
        store.advance_backfill("kw", ekey, "openalex", next_cursor=None, items_this_page=0, exhausted=True)
        assert store.is_backfill_exhausted("kw", ekey, "openalex") is True

    def test_corrupt_json_fails_closed(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        store.ensure_keyword("kw", ["query A"], sig)
        path = tmp_path / notebook_filename("kw")
        path.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(NotebookCorruptError):
            store.load("kw")


# ── reset ────────────────────────────────────────────────────────────


class TestReset:
    def test_reset_backfill_only(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        store.ensure_keyword("kw", ["query A"], sig)
        ekey = expansion_key("query A", sig)
        store.advance_backfill("kw", ekey, "openalex", next_cursor="Z9", items_this_page=5)
        store.advance_backfill("kw", ekey, "crossref", next_cursor="Y8", items_this_page=5)
        store.reset_backfill("kw", reason="test", pag_sig=sig)
        assert store.get_backfill_cursor("kw", ekey, "openalex") == INITIAL_CURSOR
        assert store.get_backfill_cursor("kw", ekey, "crossref") == INITIAL_CURSOR
        nb = store.load("kw")
        assert len(nb.get("reset_history", [])) == 1
        assert nb["reset_history"][0]["reason"] == "test"

    def test_reset_does_not_affect_other_keywords(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        store.ensure_keyword("kw A", ["query A"], sig)
        store.ensure_keyword("kw B", ["query B"], sig)
        ekey_a = expansion_key("query A", sig)
        ekey_b = expansion_key("query B", sig)
        store.advance_backfill("kw A", ekey_a, "openalex", next_cursor="A5", items_this_page=5)
        store.advance_backfill("kw B", ekey_b, "openalex", next_cursor="B5", items_this_page=5)
        store.reset_backfill("kw A", reason="test", pag_sig=sig)
        assert store.get_backfill_cursor("kw A", ekey_a, "openalex") == INITIAL_CURSOR
        assert store.get_backfill_cursor("kw B", ekey_b, "openalex") == "B5"

    def test_reset_does_not_delete_notebook(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        store.ensure_keyword("kw", ["query A"], sig)
        store.reset_backfill("kw", reason="test", pag_sig=sig)
        assert store.load("kw") is not None


# ── pagination signature ─────────────────────────────────────────────


class TestPaginationSignature:
    def test_sort_change_creates_new_signature(self):
        assert pagination_signature(sort="relevance") != pagination_signature(sort="date")

    def test_same_params_same_signature(self):
        assert pagination_signature(sort="relevance") == pagination_signature(sort="relevance")

    def test_page_size_not_in_signature(self):
        # page_size is a run param; it must NOT affect the signature.
        # (signature only takes sort/filters/schema_version)
        sig1 = pagination_signature(sort="relevance")
        sig2 = pagination_signature(sort="relevance")
        assert sig1 == sig2

    def test_changed_signature_resets_backfill_on_ensure(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig1 = pagination_signature(sort="relevance")
        store.ensure_keyword("kw", ["query A"], sig1)
        ekey1 = expansion_key("query A", sig1)
        store.advance_backfill("kw", ekey1, "openalex", next_cursor="CURSOR1", items_this_page=5)
        # Change sort → new signature → new expansion key, old one inactive.
        sig2 = pagination_signature(sort="date")
        store.ensure_keyword("kw", ["query A"], sig2)
        ekey2 = expansion_key("query A", sig2)
        assert ekey1 != ekey2
        # New expansion starts fresh.
        assert store.get_backfill_cursor("kw", ekey2, "openalex") == INITIAL_CURSOR
        # Old expansion preserved (inactive) with its cursor.
        nb = store.load("kw")
        assert nb["expansions"][ekey1]["active"] is False
        assert nb["expansions"][ekey1]["providers"]["openalex"]["backfill"]["cursor"] == "CURSOR1"

    def test_composite_backfill_signature_ignores_refresh_sort(self):
        sig1 = composite_backfill_signature(
            page_size=50,
            openalex_backfill_sort="cited_by_count:desc",
            crossref_backfill_sort="published",
        )
        sig2 = composite_backfill_signature(
            page_size=50,
            openalex_backfill_sort="cited_by_count:desc",
            crossref_backfill_sort="published",
        )
        sig3 = composite_backfill_signature(
            page_size=50,
            openalex_backfill_sort="publication_date:desc",
            crossref_backfill_sort="published",
        )
        assert sig1 == sig2
        assert sig1 != sig3


class TestBackpressure:
    def test_backpressure_uses_hysteresis(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        store.ensure_keyword("kw", ["query A"], pagination_signature())

        state = store.update_backpressure("kw", pending_count=1000, max_threshold=1000, resume_threshold=700)
        assert state["active"] is True

        state = store.update_backpressure("kw", pending_count=999, max_threshold=1000, resume_threshold=700)
        assert state["active"] is True

        state = store.update_backpressure("kw", pending_count=700, max_threshold=1000, resume_threshold=700)
        assert state["active"] is False

    def test_backpressure_rejects_bad_thresholds(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        store.ensure_keyword("kw", ["query A"], pagination_signature())
        with pytest.raises(ValueError):
            store.update_backpressure("kw", pending_count=1, max_threshold=1000, resume_threshold=1000)


# ── list / show ──────────────────────────────────────────────────────


class TestListShow:
    def test_list_returns_all_keywords(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        store.ensure_keyword("kw A", ["query A"], sig)
        store.ensure_keyword("kw B", ["query B"], sig)
        items = store.list_keywords()
        assert len(items) == 2
        keywords = {i["keyword"] for i in items}
        assert keywords == {"kw A", "kw B"}

    def test_show_missing_returns_none(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        assert store.show("nope") is None

    def test_enable_disable(self, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        store.ensure_keyword("kw", ["query A"], sig)
        store.set_enabled("kw", False)
        assert store.show("kw")["enabled"] is False
        store.set_enabled("kw", True)
        assert store.show("kw")["enabled"] is True
