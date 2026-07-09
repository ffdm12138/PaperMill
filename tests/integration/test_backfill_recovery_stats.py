"""Integration tests for Backfill recovery statistics propagation (Phase 4).

Verifies that when ``recover_last_committed_journal`` recovers a journal, the
recovery's ``pages_recovered``/``journals_recovered`` are carried into the
final transaction result on EVERY exit path (success, exhausted, page-budget
stop, provider failure), and that notebook totals are NOT double-counted.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from src.discovery.backfill_transaction import run_backfill_page_transaction
from src.discovery.keyword_notebook import KeywordNotebookStore, expansion_key
from src.discovery.models import PaperCandidate
from src.discovery.page_journal import (
    INITIAL_CURSOR,
    PageJournalStore,
    backfill_page_id,
    request_signature,
)


pytestmark = pytest.mark.integration


@dataclass
class _Page:
    candidates: list[PaperCandidate]
    next_cursor: str | None
    exhausted: bool = False
    status: str = "success"
    safe_error: str | None = None
    error_type: str | None = None

    @property
    def returned_count(self) -> int:
        return len(self.candidates)


def _setup_recovery(tmp_path: Path, *, exhausted: bool = False, next_cursor="NEXT"):
    """Write a fetched journal + commit its cursor, but do NOT mark it committed.

    This simulates a crash between the cursor CAS and the journal state mark,
    leaving a journal that ``recover_last_committed_journal`` must recover.
    """
    notebook = KeywordNotebookStore(tmp_path / "notebooks")
    journal = PageJournalStore(tmp_path / "pages")
    sig = request_signature(page_size=10)
    notebook.ensure_keyword("kw", ["kw"], sig["hash"])
    nb = notebook.require("kw")
    ekey = expansion_key("kw", sig["hash"])
    pid = backfill_page_id(
        keyword_id=nb["keyword_id"], expansion_id=ekey, provider="openalex",
        request_signature_hash=sig["hash"], request_cursor=INITIAL_CURSOR,
    )
    page_path = journal.write_page(journal.make_page(
        page_id=pid,
        keyword_id=nb["keyword_id"],
        keyword="kw",
        expansion_id=ekey,
        expanded_query="kw",
        provider="openalex",
        lane="backfill",
        request_signature_value=sig,
        request_cursor=INITIAL_CURSOR,
        next_cursor=next_cursor,
        provider_exhausted=exhausted,
        candidates=[PaperCandidate(title="T", doi="10.1234/recover")],
        state="fetched",
    ))
    notebook.commit_backfill_cursor(
        "kw", ekey, "openalex",
        expected_cursor=INITIAL_CURSOR,
        next_cursor=next_cursor,
        committed_page_id=pid,
        exhausted=exhausted,
        items_this_page=1,
    )
    return notebook, journal, sig, nb, ekey, pid, page_path


def _bf_state(notebook, ekey):
    return notebook.get_backfill_state("kw", ekey, "openalex")


def test_recover_then_fetch_next_page_carries_recovery_stats(tmp_path: Path):
    notebook, journal, sig, nb, ekey, pid, page_path = _setup_recovery(tmp_path)
    items_before = _bf_state(notebook, ekey)["items_returned_total"]
    pages_committed_before = _bf_state(notebook, ekey)["pages_committed"]

    def fetch(*a, **k):
        return _Page([PaperCandidate(title="T2", doi="10.1234/next")], next_cursor="NEXT2")

    result = run_backfill_page_transaction(
        keyword="kw", keyword_id=nb["keyword_id"], expansion_id=ekey,
        expanded_query="kw", provider="openalex",
        notebook_store=notebook, journal_store=journal,
        locks_dir=tmp_path / "locks", request_signature=sig, page_size=10,
        fetch_page=fetch,
    )
    assert result.status == "success"
    assert result.pages_recovered == 1
    assert result.journals_recovered == 1
    assert result.pages_requested == 1
    # Notebook totals were advanced by the pre-crash CAS; recovery must NOT
    # re-increment them.
    state = _bf_state(notebook, ekey)
    assert state["items_returned_total"] == items_before + 1  # only the new page
    assert state["pages_committed"] == pages_committed_before + 1


def test_recover_then_exhausted_carries_recovery_stats(tmp_path: Path):
    # The recovered journal itself signals provider_exhausted. After recovery
    # marks it cursor_committed, is_backfill_exhausted returns True.
    notebook, journal, sig, nb, ekey, pid, page_path = _setup_recovery(
        tmp_path, exhausted=True, next_cursor=None,
    )

    def fetch(*a, **k):
        pytest.fail("should not fetch — provider is exhausted after recovery")

    result = run_backfill_page_transaction(
        keyword="kw", keyword_id=nb["keyword_id"], expansion_id=ekey,
        expanded_query="kw", provider="openalex",
        notebook_store=notebook, journal_store=journal,
        locks_dir=tmp_path / "locks", request_signature=sig, page_size=10,
        fetch_page=fetch,
    )
    assert result.status == "exhausted"
    assert result.pages_recovered == 1
    assert result.journals_recovered == 1


def test_recover_then_page_budget_stop_carries_recovery_stats(tmp_path: Path):
    notebook, journal, sig, nb, ekey, pid, page_path = _setup_recovery(tmp_path)

    def fetch(*a, **k):
        page = _Page([], next_cursor=None, exhausted=False)
        page.status = "failed"
        page.error_type = "page_budget_exhausted"
        return page

    result = run_backfill_page_transaction(
        keyword="kw", keyword_id=nb["keyword_id"], expansion_id=ekey,
        expanded_query="kw", provider="openalex",
        notebook_store=notebook, journal_store=journal,
        locks_dir=tmp_path / "locks", request_signature=sig, page_size=10,
        fetch_page=fetch,
    )
    assert result.status == "stopped"
    assert result.stop_reason == "page_budget_exhausted"
    assert result.pages_recovered == 1
    assert result.journals_recovered == 1


def test_recover_then_provider_failure_carries_recovery_stats(tmp_path: Path):
    notebook, journal, sig, nb, ekey, pid, page_path = _setup_recovery(tmp_path)

    def fetch(*a, **k):
        page = _Page([], next_cursor=None)
        page.status = "failed"
        page.safe_error = "provider down"
        return page

    result = run_backfill_page_transaction(
        keyword="kw", keyword_id=nb["keyword_id"], expansion_id=ekey,
        expanded_query="kw", provider="openalex",
        notebook_store=notebook, journal_store=journal,
        locks_dir=tmp_path / "locks", request_signature=sig, page_size=10,
        fetch_page=fetch,
    )
    assert result.status == "failed_retryable"
    assert result.error_type == "provider_retryable"
    assert result.pages_recovered == 1
    assert result.journals_recovered == 1


def test_no_recovery_when_clean_has_zero_recovery_stats(tmp_path: Path):
    """A clean run (nothing to recover) reports zero recovery stats."""
    notebook = KeywordNotebookStore(tmp_path / "notebooks")
    journal = PageJournalStore(tmp_path / "pages")
    sig = request_signature(page_size=10)
    notebook.ensure_keyword("kw", ["kw"], sig["hash"])
    nb = notebook.require("kw")
    ekey = expansion_key("kw", sig["hash"])

    def fetch(*a, **k):
        return _Page([PaperCandidate(title="T", doi="10.1/x")], next_cursor="NEXT")

    result = run_backfill_page_transaction(
        keyword="kw", keyword_id=nb["keyword_id"], expansion_id=ekey,
        expanded_query="kw", provider="openalex",
        notebook_store=notebook, journal_store=journal,
        locks_dir=tmp_path / "locks", request_signature=sig, page_size=10,
        fetch_page=fetch,
    )
    assert result.status == "success"
    assert result.pages_recovered == 0
    assert result.journals_recovered == 0
    assert result.pages_requested == 1
