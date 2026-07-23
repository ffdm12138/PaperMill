"""JournalIndexV4 — rebuildable index over page journals.

The index is NOT the fact source — page journals and candidate receipts
are the fact source.  The index can be fully rebuilt from canonical V4 stores.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from src.discovery.stores.page_journal_store import PageJournalStoreV4


@dataclass
class JournalIndexV4:
    """Rebuildable index over v4 page journals.

    Tracks: page_id → file path, keyword_id → set of page_ids,
    drained status, and candidate counts.
    """

    _store: PageJournalStoreV4
    _by_page_id: dict[str, Path] = field(default_factory=dict)
    _by_keyword: dict[str, set[str]] = field(default_factory=dict)
    _drained_pages: set[str] = field(default_factory=set)
    _total_candidates: int = 0

    @classmethod
    def build(cls, store: PageJournalStoreV4) -> "JournalIndexV4":
        """Rebuild the index from scratch by scanning all v4 page journals."""
        index = cls(_store=store)
        for path in store.list_all():
            journal = store.read(path)
            if journal is None:
                continue
            pid = journal.page_id
            if not pid:
                continue
            index._by_page_id[pid] = path
            kid = journal.keyword_id
            if kid:
                index._by_keyword.setdefault(kid, set()).add(pid)
            if journal.state == "drained":
                index._drained_pages.add(pid)
            index._total_candidates += len(journal.candidates)
        return index

    @property
    def page_count(self) -> int:
        return len(self._by_page_id)

    @property
    def drained_count(self) -> int:
        return len(self._drained_pages)

    @property
    def total_candidates(self) -> int:
        return self._total_candidates

    def page_ids_for_keyword(self, keyword_id: str) -> set[str]:
        return self._by_keyword.get(keyword_id, set())

    def is_drained(self, page_id: str) -> bool:
        return page_id in self._drained_pages

    def mark_drained(self, page_id: str) -> None:
        self._drained_pages.add(page_id)

    def add_page(self, page_id: str, path: Path, keyword_id: str,
                 candidate_count: int) -> None:
        self._by_page_id[page_id] = path
        self._by_keyword.setdefault(keyword_id, set()).add(page_id)
        self._total_candidates += candidate_count
