"""Notebook schema extension tests (discovery execution contract).

Covers:
- Pre-contract notebooks (no window fields, no exhaustion_evidence) remain
  readable — additive extensions are optional.
- New fields round-trip through validate_notebook.
- commit_backfill_cursor refuses exhausted=True without evidence.
- record_backfill_error actually writes the backoff schedule.
"""
from __future__ import annotations

import copy

import pytest

from src.discovery.contracts.notebook import (
    NotebookCorruptError,
    _empty_search_query,
    empty_notebook,
    validate_notebook,
)
from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore


def _nb_with_query() -> dict:
    nb = empty_notebook("风蚀测试")
    entry = _empty_search_query("风蚀", "0123456789abcdef")
    nb["search_queries"][entry["query_id"]] = entry
    return nb


def test_precontract_notebook_remains_valid() -> None:
    """A notebook missing all extension fields still validates."""
    nb = _nb_with_query()
    qid = next(iter(nb["search_queries"]))
    for provider in ("openalex", "crossref"):
        lanes = nb["search_queries"][qid]["providers"][provider]
        # Strip every extension field to mimic a pre-contract notebook.
        for key in (
            "last_window_completed_at", "last_window_pages",
            "last_window_signature", "last_window_page_ids",
            "consecutive_failures", "next_retry_at",
        ):
            lanes["refresh"].pop(key, None)
    validate_notebook(copy.deepcopy(nb))  # must not raise


def test_refresh_window_fields_roundtrip() -> None:
    nb = _nb_with_query()
    qid = next(iter(nb["search_queries"]))
    refresh = nb["search_queries"][qid]["providers"]["openalex"]["refresh"]
    refresh.update(
        last_window_completed_at="2026-07-21T00:00:00+00:00",
        last_window_pages=2,
        last_window_signature="0123456789abcdef",
        last_window_page_ids=["p0", "p1"],
        consecutive_failures=0,
        next_retry_at=None,
    )
    validated = validate_notebook(copy.deepcopy(nb))
    r = validated["search_queries"][qid]["providers"]["openalex"]["refresh"]
    assert r["last_window_signature"] == "0123456789abcdef"
    assert r["last_window_page_ids"] == ["p0", "p1"]


def test_refresh_rejects_unknown_keys() -> None:
    nb = _nb_with_query()
    qid = next(iter(nb["search_queries"]))
    nb["search_queries"][qid]["providers"]["openalex"]["refresh"]["bogus"] = 1
    with pytest.raises(NotebookCorruptError):
        validate_notebook(nb)


def test_backfill_exhaustion_evidence_roundtrip() -> None:
    nb = _nb_with_query()
    qid = next(iter(nb["search_queries"]))
    bf = nb["search_queries"][qid]["providers"]["openalex"]["backfill"]
    bf["request_signature"] = "0123456789abcdef"
    bf["exhausted"] = True
    bf["exhaustion_evidence"] = {
        "provider": "openalex",
        "query_id": qid,
        "request_signature": "0123456789abcdef",
        "generation": 1,
        "cursor_before": "*",
        "response_metadata": {
            "http_status": 200,
            "provider_request_id": None,
            "retry_after_observed": None,
            "total_results": 0,
            "next_cursor_present": False,
            "response_fingerprint": "abcdef0123456789",
            "observed_at": "2026-07-21T00:00:00+00:00",
        },
        "observed_at": "2026-07-21T00:00:00+00:00",
    }
    validated = validate_notebook(copy.deepcopy(nb))
    ev = validated["search_queries"][qid]["providers"]["openalex"]["backfill"][
        "exhaustion_evidence"
    ]
    assert ev["response_metadata"]["next_cursor_present"] is False


def test_backfill_rejects_unknown_extension_key() -> None:
    nb = _nb_with_query()
    qid = next(iter(nb["search_queries"]))
    bf = nb["search_queries"][qid]["providers"]["openalex"]["backfill"]
    bf["request_signature"] = "0123456789abcdef"
    bf["bogus_extension"] = True
    with pytest.raises(NotebookCorruptError):
        validate_notebook(nb)


def _make_store(tmp_path):
    """Create a notebook with one query and return (store, keyword, qid)."""
    store = KeywordNotebookStore(tmp_path)
    store.create_notebook(
        "风蚀测试",
        search_queries=[{"query": "风蚀", "language": "zh"}],
        enabled=False,
    )
    nb = store.require_v4("风蚀测试")
    qid = next(iter(nb["search_queries"]))
    return store, "风蚀测试", qid


def test_commit_backfill_cursor_requires_evidence(tmp_path) -> None:
    store, kw, qid = _make_store(tmp_path)
    with pytest.raises(ValueError, match="exhaustion_evidence"):
        store.commit_backfill_cursor(
            kw, qid, "openalex",
            expected_cursor="*", next_cursor=None,
            committed_page_id="p0", exhausted=True,
        )


def test_record_backfill_error_writes_backoff_schedule(tmp_path) -> None:
    store, kw, qid = _make_store(tmp_path)
    store.ensure_backfill_generation(
        kw, qid, "openalex", request_signature_hash="0123456789abcdef"
    )
    store.record_backfill_error(
        kw, qid, "openalex",
        error="HTTP 500", error_type="ProviderTransientError",
        backoff_seconds=60.0,
    )
    bf = store.get_backfill_state(kw, qid, "openalex")
    assert bf["consecutive_failures"] == 1
    assert bf["last_error"] == "HTTP 500"
    assert bf["last_error_type"] == "ProviderTransientError"
    assert bf["last_failure_at"] is not None
    assert bf["next_retry_at"] is not None
    assert bf["terminal_failure"] is False


def test_record_backfill_error_terminal(tmp_path) -> None:
    store, kw, qid = _make_store(tmp_path)
    store.ensure_backfill_generation(
        kw, qid, "openalex", request_signature_hash="0123456789abcdef"
    )
    store.record_backfill_error(
        kw, qid, "openalex",
        error="HTTP 400", error_type="ProviderPermanentError", terminal=True,
    )
    bf = store.get_backfill_state(kw, qid, "openalex")
    assert bf["terminal_failure"] is True
    assert bf["terminal_failure_at"] is not None


def test_successful_commit_clears_backoff(tmp_path) -> None:
    store, kw, qid = _make_store(tmp_path)
    store.ensure_backfill_generation(
        "风蚀测试", qid, "openalex", request_signature_hash="0123456789abcdef"
    )
    store.record_backfill_error(
        "风蚀测试", qid, "openalex", error="HTTP 500",
        error_type="ProviderTransientError", backoff_seconds=60.0,
    )
    store.commit_backfill_cursor(
        "风蚀测试", qid, "openalex",
        expected_cursor="*", next_cursor="cursor-2",
        committed_page_id="p0", exhausted=False, items_this_page=3,
    )
    bf = store.get_backfill_state("风蚀测试", qid, "openalex")
    assert bf["consecutive_failures"] == 0
    assert bf["next_retry_at"] is None
    assert bf["last_error_type"] is None
    assert bf["cursor"] == "cursor-2"
