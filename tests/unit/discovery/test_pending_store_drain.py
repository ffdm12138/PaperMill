"""Unit tests for ``PendingCandidateStoreV4`` consumption in the staging drain.

The production staging drain (``drain_pending_candidates`` with
``pending_store=``) consumes strict ``PendingCandidateV4`` files in
addition to page-journal candidates.  Candidate files are deleted only
after the gateway reaches a durable result; failures keep the file so a
later drain retries it.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from src.discovery.contracts.candidate import PendingCandidateV4
from src.discovery.pending_queue import drain_pending_candidates
from src.discovery.runtime.batch_runtime import ActiveRelevanceProfiles, DiscoveryBatchRuntime
from src.discovery.staging_gateway import MetadataStagingBatchResultV4
from src.discovery.stores.page_journal_store import PageJournalStoreV4
from src.discovery.stores.pending_candidate_store import PendingCandidateStoreV4

pytestmark = pytest.mark.unit

KEYWORD_ID = "kw-pending-store-drain"
PROFILE_HASH = "test-active-profile"


def _store(root: Path) -> PendingCandidateStoreV4:
    workspace = types.SimpleNamespace(pending_candidates_dir=root / "pending_candidates")
    return PendingCandidateStoreV4(workspace)


def _pending(candidate_id: str, doi: str, title: str) -> PendingCandidateV4:
    return PendingCandidateV4(
        candidate_id=candidate_id,
        keyword_id=KEYWORD_ID,
        origin="legacy_candidate_seed",
        doi=doi,
        normalized_doi=doi,
        title=title,
        authors=["Au"],
        year=2021,
        raw_provider_data={"provider": "openalex"},
        created_at="2026-01-01T00:00:00+00:00",
    )


def _runtime(tmp_path: Path, journal: PageJournalStoreV4) -> DiscoveryBatchRuntime:
    return DiscoveryBatchRuntime.create(
        journal=journal,
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        needs_staging=True,
        active_relevance_profiles=ActiveRelevanceProfiles.build({KEYWORD_ID: PROFILE_HASH}),
    )


class FakeGateway:
    """Records stage_batch calls; fails DOIs listed in ``failing``."""

    def __init__(self, failing: frozenset[str] = frozenset()) -> None:
        self.records: list[dict] = []
        self.calls = 0
        self.failing = set(failing)

    def stage_batch(self, records, *, apply, skip_duplicates, transaction):
        records = [dict(r) for r in records]
        self.calls += 1
        self.records.extend(records)
        items = []
        for idx, record in enumerate(records):
            doi = record["discovery_context"]["normalized_doi"]
            if doi in self.failing:
                items.append({"status": "failed_retryable", "safe_error": "boom"})
            else:
                items.append({
                    "status": "staged",
                    "actual_allocated": True,
                    "paper_number": f"{idx:016d}",
                })
        return MetadataStagingBatchResultV4(
            staged_new=sum(1 for item in items if item["status"] == "staged"),
            failed_retryable=sum(1 for item in items if item["status"] == "failed_retryable"),
            items=tuple(items),
        )


def _drain(tmp_path: Path, journal, runtime, store, gateway, budget: int = 16):
    return drain_pending_candidates(
        journal=journal,
        keyword_ids=[KEYWORD_ID],
        candidate_budget=budget,
        stage_to_paper_raw=True,
        apply=True,
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        locks_dir=tmp_path / "locks",
        exports_dir=tmp_path / "exports",
        worker_id="worker",
        runtime=runtime,
        gateway=gateway,
        pending_store=store,
    )


def test_pending_candidates_are_staged_and_deleted(tmp_path: Path):
    store = _store(tmp_path)
    for cid, doi, title in [
        ("c1", "10.5555/alpha", "Alpha"),
        ("c2", "10.5555/beta", "Beta"),
        ("c3", "10.5555/gamma", "Gamma"),
    ]:
        store.write(_pending(cid, doi, title))
    journal = PageJournalStoreV4(tmp_path / "pages")
    gateway = FakeGateway()

    report = _drain(tmp_path, journal, _runtime(tmp_path, journal), store, gateway)

    assert report.errors == []
    assert report.staged == 3
    assert report.remaining == 0
    assert gateway.calls == 1
    assert sorted(r["doi"] for r in gateway.records) == [
        "10.5555/alpha", "10.5555/beta", "10.5555/gamma",
    ]
    assert all(
        r["discovery_context"]["keyword_id"] == KEYWORD_ID for r in gateway.records
    )
    assert sorted(r["discovery_context"]["normalized_doi"] for r in gateway.records) == [
        "10.5555/alpha", "10.5555/beta", "10.5555/gamma",
    ]
    assert store.count() == 0


def test_failed_candidates_are_kept_for_retry(tmp_path: Path):
    store = _store(tmp_path)
    for cid, doi, title in [
        ("c1", "10.5555/alpha", "Alpha"),
        ("c2", "10.5555/beta", "Beta"),
        ("c3", "10.5555/gamma", "Gamma"),
    ]:
        store.write(_pending(cid, doi, title))
    journal = PageJournalStoreV4(tmp_path / "pages")
    gateway = FakeGateway(failing=frozenset({"10.5555/beta"}))

    report = _drain(tmp_path, journal, _runtime(tmp_path, journal), store, gateway)

    assert report.staged == 2
    assert report.retryable_failures == 1
    remaining = store.list_all()
    assert len(remaining) == 1
    assert remaining[0].stem == "c2"


def test_second_drain_does_not_restage_consumed_candidates(tmp_path: Path):
    store = _store(tmp_path)
    store.write(_pending("c1", "10.5555/alpha", "Alpha"))
    store.write(_pending("c2", "10.5555/beta", "Beta"))
    journal = PageJournalStoreV4(tmp_path / "pages")
    runtime = _runtime(tmp_path, journal)
    gateway = FakeGateway()

    first = _drain(tmp_path, journal, runtime, store, gateway)
    assert first.staged == 2
    assert gateway.calls == 1
    assert store.count() == 0

    second = _drain(tmp_path, journal, runtime, store, gateway)
    assert second.processed == 0
    assert second.staged == 0
    assert gateway.calls == 1


def test_drain_budget_bounds_pending_consumption(tmp_path: Path):
    store = _store(tmp_path)
    for cid, doi, title in [
        ("c1", "10.5555/alpha", "Alpha"),
        ("c2", "10.5555/beta", "Beta"),
        ("c3", "10.5555/gamma", "Gamma"),
    ]:
        store.write(_pending(cid, doi, title))
    journal = PageJournalStoreV4(tmp_path / "pages")
    gateway = FakeGateway()

    report = _drain(tmp_path, journal, _runtime(tmp_path, journal), store, gateway, budget=2)

    assert report.staged == 2
    assert report.remaining == 1
    assert store.count() == 1


def test_same_doi_pending_candidates_stage_once(tmp_path: Path):
    store = _store(tmp_path)
    store.write(_pending("c1", "10.5555/same", "Same"))
    store.write(_pending("c2", "10.5555/same", "Same duplicate"))
    journal = PageJournalStoreV4(tmp_path / "pages")
    gateway = FakeGateway()

    report = _drain(tmp_path, journal, _runtime(tmp_path, journal), store, gateway)

    assert report.staged == 1
    assert report.duplicate_observation == 1
    assert len(gateway.records) == 1
    assert store.count() == 0
