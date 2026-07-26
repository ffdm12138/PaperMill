"""Integration: migrated legacy pending candidates enter the v4 drain chain.

The migrator writes strict ``PendingCandidateV4`` files into the staging
workspace's ``pending_candidates/`` store.  The production drain chain
(``CandidateDrainCoordinator.drain`` → ``drain_pending_candidates`` →
``MetadataStagingGateway.stage_batch``) now consumes that store directly,
so this test drains the migrated candidates through the real production
path with a fake gateway — no synthetic page-journal binding.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from src.discovery.contracts.notebook import keyword_id, query_identity
from src.discovery.runtime.batch_runtime import ActiveRelevanceProfiles, DiscoveryBatchRuntime
from src.discovery.runtime.candidate_drain import CandidateDrainCoordinator
from src.discovery.staging_gateway import MetadataStagingBatchResultV4
from src.discovery.stores.page_journal_store import PageJournalStoreV4
from src.discovery.stores.pending_candidate_store import PendingCandidateStoreV4

import scripts.migrate_discovery_v4 as migrate_mod
import src.discovery.workspace as workspace_mod
import src.migrations.discovery_v4.migration_journal as journal_mod

pytestmark = pytest.mark.integration

KEYWORD_ZH = "迁移关键词"
KEYWORD_ID = keyword_id(KEYWORD_ZH)
PROFILE_HASH = "test-active-profile"


@pytest.fixture
def iso_migration(tmp_path, monkeypatch):
    """Patch migrator/journal/workspace directories into isolated tmp paths."""
    discovery_dir = tmp_path / "discovery"
    migrations_dir = discovery_dir / "migrations"
    staging_dir = discovery_dir / "generations" / ".staging"
    for d in [migrations_dir, staging_dir]:
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(migrate_mod, "DISCOVERY_DIR", discovery_dir)
    monkeypatch.setattr(migrate_mod, "PAPER_NUMBER_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(migrate_mod, "PAPERS_DIR", tmp_path / "papers")
    monkeypatch.setattr(migrate_mod, "PAPER_RAW_DIR", tmp_path / "paper_raw")
    monkeypatch.setattr(journal_mod, "DISCOVERY_MIGRATIONS_DIR", migrations_dir)
    monkeypatch.setattr(workspace_mod, "STAGING_DIR", staging_dir)

    return types.SimpleNamespace(tmp_path=tmp_path, discovery_dir=discovery_dir)


def _write_legacy_page(pages_dir: Path, name: str, candidates: list[dict]) -> None:
    from tests.helpers.legacy_journals import make_journal

    path = pages_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    page = make_journal(
        candidates,
        keyword_id=KEYWORD_ID,
        keyword_zh=KEYWORD_ZH,
        query_id=query_identity("zh", KEYWORD_ZH),
        query=KEYWORD_ZH,
        query_language="zh",
        provider="openalex",
        lane="backfill",
        page_id=path.stem,
    )
    path.write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")


def _candidate(doi: str, candidate_id: str, title: str) -> dict:
    from tests.helpers.legacy_journals import make_candidate

    wrapper = make_candidate(
        "pending", doi, candidate_id=candidate_id, relevance_state="passed",
    )
    wrapper["candidate"]["title"] = title
    wrapper["candidate"]["year"] = 2021
    return wrapper


def test_migrated_pending_candidates_are_consumed_by_drain(iso_migration, tmp_path):
    mid = "drain-integration"
    staging_ws = migrate_mod.create_staging_workspace(mid)
    journal = journal_mod.MigrationJournal.create(mid)
    for s in [
        journal_mod.MigrationState.INVENTORY_COMPLETE,
        journal_mod.MigrationState.ARCHIVE_PREPARED,
        journal_mod.MigrationState.WORKSPACE_BUILT,
        journal_mod.MigrationState.NOTEBOOKS_STAGED,
    ]:
        journal.transition_to(s)
    journal.metadata["staging_workspace"] = str(staging_ws.root)
    journal.save()

    notebook = {
        "schema_version": "4.0",
        "keyword_id": KEYWORD_ID,
        "keyword_zh": KEYWORD_ZH,
        "enabled": True,
        "search_queries": {},
    }
    (staging_ws.keyword_notebook_dir / f"{KEYWORD_ZH}__{KEYWORD_ID[:8]}.json").write_text(
        json.dumps(notebook, ensure_ascii=False), encoding="utf-8"
    )

    pages_dir = iso_migration.discovery_dir / "legacy_archive" / mid / "pending_pages"
    _write_legacy_page(pages_dir, "kw/p1.json", [
        _candidate("10.5555/alpha", "c1", "Alpha"),
        _candidate("10.5555/beta", "c2", "Beta"),
        _candidate("10.5555/gamma", "c3", "Gamma"),
    ])

    report = migrate_mod._step_extract_candidates(
        journal, staging_ws, types.SimpleNamespace(),
    )
    assert report.imported == 3
    assert report.errors == []

    # Every migrated pending candidate must strict-read back from the store.
    pending_store = PendingCandidateStoreV4(staging_ws)
    pending = []
    for path in pending_store.list_all():
        loaded = pending_store.read(path.parent.name, path.stem)
        assert loaded is not None, f"pending candidate not strict-readable: {path}"
        pending.append(loaded)
    assert len(pending) == 3
    assert {pc.origin for pc in pending} == {"legacy_candidate_seed"}

    # Production consumption path: CandidateDrainCoordinator.drain →
    # drain_pending_candidates(pending_store=…) → gateway.stage_batch.
    journal_store = PageJournalStoreV4(tmp_path / "pages")
    runtime = DiscoveryBatchRuntime.create(
        journal=journal_store,
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        needs_staging=True,
        active_relevance_profiles=ActiveRelevanceProfiles.build({KEYWORD_ID: PROFILE_HASH}),
    )

    staged_records: list[dict] = []

    class FakeGateway:
        def stage_batch(self, records, *, apply, skip_duplicates, transaction):
            records = list(records)
            staged_records.extend(dict(r) for r in records)
            return MetadataStagingBatchResultV4(
                staged_new=len(records),
                items=tuple(
                    {"status": "staged", "actual_allocated": True,
                     "paper_number": f"{idx:016d}"}
                    for idx, _ in enumerate(records)
                ),
            )

    drain = CandidateDrainCoordinator(
        runtime=runtime,
        journal=journal_store,
        worker_id="worker",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        locks_dir=tmp_path / "locks",
        exports_dir=tmp_path / "exports",
        gateway=FakeGateway(),
        pending_store=pending_store,
        stage_to_paper_raw=True,
        apply=True,
    )
    drain_report = drain.drain(KEYWORD_ID, 8, phase="final")

    assert drain_report.errors == []
    assert drain_report.staged == 3
    assert sorted(r["doi"] for r in staged_records) == sorted(
        pc.normalized_doi for pc in pending
    )
    assert all(
        r["discovery_context"]["keyword_id"] == KEYWORD_ID for r in staged_records
    )
    # Consumed candidates are removed from the store; a second drain is a no-op.
    assert pending_store.count() == 0
    second = drain.drain(KEYWORD_ID, 8, phase="final")
    assert second.processed == 0
    assert len(staged_records) == 3
