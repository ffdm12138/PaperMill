from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from src.discovery.backfill_transaction import run_backfill_page_transaction
from src.discovery.keyword_notebook import KeywordNotebookStore, expansion_key
from src.discovery.models import PaperCandidate
from src.discovery.page_journal import INITIAL_CURSOR, PageJournalStore, request_signature


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


def test_backfill_transaction_journal_first_then_recovers_without_refetch(tmp_path: Path):
    notebook = KeywordNotebookStore(tmp_path / "notebooks")
    journal = PageJournalStore(tmp_path / "pages")
    sig = request_signature(page_size=10)
    notebook.ensure_keyword("kw", ["kw"], sig["hash"])
    ekey = expansion_key("kw", sig["hash"])
    calls = 0

    def fetch(*args, **kwargs):
        nonlocal calls
        calls += 1
        return _Page([PaperCandidate(title="T", doi="10.1234/txn")], next_cursor="NEXT")

    result = run_backfill_page_transaction(
        keyword="kw",
        keyword_id=notebook.require("kw")["keyword_id"],
        expansion_id=ekey,
        expanded_query="kw",
        provider="openalex",
        notebook_store=notebook,
        journal_store=journal,
        locks_dir=tmp_path / "locks",
        request_signature=sig,
        page_size=10,
        fetch_page=fetch,
    )
    assert result.pages_requested == 1
    assert result.pages_persisted == 1
    assert result.pages_committed == 1
    assert notebook.get_backfill_cursor("kw", ekey, "openalex") == "NEXT"

    result2 = run_backfill_page_transaction(
        keyword="kw",
        keyword_id=notebook.require("kw")["keyword_id"],
        expansion_id=ekey,
        expanded_query="kw",
        provider="openalex",
        notebook_store=notebook,
        journal_store=journal,
        locks_dir=tmp_path / "locks",
        request_signature=sig,
        page_size=10,
        fetch_page=fetch,
    )
    assert calls == 2
    assert result2.pages_requested == 1


def test_existing_fetched_journal_commit_does_not_consume_network(tmp_path: Path):
    notebook = KeywordNotebookStore(tmp_path / "notebooks")
    journal = PageJournalStore(tmp_path / "pages")
    sig = request_signature(page_size=10)
    notebook.ensure_keyword("kw", ["kw"], sig["hash"])
    nb = notebook.require("kw")
    ekey = expansion_key("kw", sig["hash"])
    from src.discovery.page_journal import backfill_page_id

    pid = backfill_page_id(keyword_id=nb["keyword_id"], expansion_id=ekey, provider="openalex", request_signature_hash=sig["hash"], request_cursor=INITIAL_CURSOR)
    journal.write_page(journal.make_page(
        page_id=pid,
        keyword_id=nb["keyword_id"],
        keyword="kw",
        expansion_id=ekey,
        expanded_query="kw",
        provider="openalex",
        lane="backfill",
        request_signature_value=sig,
        request_cursor=INITIAL_CURSOR,
        next_cursor="RECOVERED",
        provider_exhausted=False,
        candidates=[PaperCandidate(title="T", doi="10.1234/recovered")],
        state="fetched",
    ))

    result = run_backfill_page_transaction(
        keyword="kw",
        keyword_id=nb["keyword_id"],
        expansion_id=ekey,
        expanded_query="kw",
        provider="openalex",
        notebook_store=notebook,
        journal_store=journal,
        locks_dir=tmp_path / "locks",
        request_signature=sig,
        page_size=10,
        fetch_page=lambda *a, **k: pytest.fail("existing journal should be recovered"),
    )
    assert result.pages_recovered == 1
    assert result.pages_requested == 0
    assert notebook.get_backfill_cursor("kw", ekey, "openalex") == "RECOVERED"


def test_cursor_committed_before_journal_mark_is_recovered(tmp_path: Path):
    notebook = KeywordNotebookStore(tmp_path / "notebooks")
    journal = PageJournalStore(tmp_path / "pages")
    sig = request_signature(page_size=10)
    notebook.ensure_keyword("kw", ["kw"], sig["hash"])
    nb = notebook.require("kw")
    ekey = expansion_key("kw", sig["hash"])
    from src.discovery.page_journal import backfill_page_id

    pid = backfill_page_id(keyword_id=nb["keyword_id"], expansion_id=ekey, provider="openalex", request_signature_hash=sig["hash"], request_cursor=INITIAL_CURSOR)
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
        next_cursor="NEXT",
        provider_exhausted=False,
        candidates=[PaperCandidate(title="T", doi="10.1234/recovered")],
        state="fetched",
    ))
    notebook.commit_backfill_cursor(
        "kw",
        ekey,
        "openalex",
        expected_cursor=INITIAL_CURSOR,
        next_cursor="NEXT",
        committed_page_id=pid,
        exhausted=False,
        items_this_page=1,
    )
    calls: list[str] = []

    def fetch(*args, **kwargs):
        calls.append(kwargs["cursor"])
        return _Page([PaperCandidate(title="T2", doi="10.1234/next")], next_cursor="NEXT2")

    result = run_backfill_page_transaction(
        keyword="kw",
        keyword_id=nb["keyword_id"],
        expansion_id=ekey,
        expanded_query="kw",
        provider="openalex",
        notebook_store=notebook,
        journal_store=journal,
        locks_dir=tmp_path / "locks",
        request_signature=sig,
        page_size=10,
        fetch_page=fetch,
    )

    assert journal.read(page_path)["state"] == "cursor_committed"
    assert calls == ["NEXT"]
    assert result.status == "success"


def test_exhausted_cursor_commit_recovery_is_noop_then_exhausted(tmp_path: Path):
    notebook = KeywordNotebookStore(tmp_path / "notebooks")
    journal = PageJournalStore(tmp_path / "pages")
    sig = request_signature(page_size=10)
    notebook.ensure_keyword("kw", ["kw"], sig["hash"])
    nb = notebook.require("kw")
    ekey = expansion_key("kw", sig["hash"])
    from src.discovery.page_journal import backfill_page_id

    pid = backfill_page_id(keyword_id=nb["keyword_id"], expansion_id=ekey, provider="openalex", request_signature_hash=sig["hash"], request_cursor=INITIAL_CURSOR)
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
        next_cursor=None,
        provider_exhausted=True,
        candidates=[PaperCandidate(title="T", doi="10.1234/end")],
        state="fetched",
    ))
    notebook.commit_backfill_cursor(
        "kw",
        ekey,
        "openalex",
        expected_cursor=INITIAL_CURSOR,
        next_cursor=None,
        committed_page_id=pid,
        exhausted=True,
        items_this_page=1,
    )

    result = run_backfill_page_transaction(
        keyword="kw",
        keyword_id=nb["keyword_id"],
        expansion_id=ekey,
        expanded_query="kw",
        provider="openalex",
        notebook_store=notebook,
        journal_store=journal,
        locks_dir=tmp_path / "locks",
        request_signature=sig,
        page_size=10,
        fetch_page=lambda *a, **k: pytest.fail("exhausted recovery must not fetch"),
    )
    assert journal.read(page_path)["state"] == "cursor_committed"
    assert result.status == "exhausted"
    assert result.stop_reason == "provider_exhausted"
