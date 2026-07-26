"""Unit tests for the Discovery v4 migration CLI."""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import threading
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import scripts.discover_papers_concurrent as discover_mod  # used for smoke monkeypatch
from filelock import FileLock

from src.discovery.contracts.candidate import PendingCandidateV4
from src.discovery.contracts.manifest import ActiveGenerationPointerV4
from src.discovery.contracts.notebook import _empty_search_query, empty_notebook
from src.discovery.stores.pending_candidate_store import PendingCandidateStoreV4
from tests.helpers.relevance_profiles import relevance_profile as _valid_relevance_profile


@pytest.fixture
def iso_discovery(tmp_path, monkeypatch):
    """Provide isolated discovery directories and reload modules with patched paths."""
    import config.settings as settings
    import src.discovery.workspace as workspace_mod
    import src.migrations.discovery_v4.archive_builder as archive_mod
    import src.migrations.discovery_v4.candidate_extraction as candidate_mod
    import src.migrations.discovery_v4.migration_journal as journal_mod

    discovery_dir = tmp_path / "discovery"
    migrations_dir = discovery_dir / "migrations"
    legacy_archive_dir = discovery_dir / "legacy_archive"
    generations_dir = discovery_dir / "generations"
    staging_dir = generations_dir / ".staging"
    active_generation_path = discovery_dir / "active_generation.json"
    keyword_notebook_dir = discovery_dir / "keyword_notebooks"
    pending_pages_dir = discovery_dir / "pending_pages"
    catalog_dir = tmp_path / "catalog"
    papers_dir = tmp_path / "papers"
    paper_raw_dir = tmp_path / "paper_raw"

    for d in [
        discovery_dir, migrations_dir, legacy_archive_dir, generations_dir, staging_dir,
        keyword_notebook_dir, pending_pages_dir, catalog_dir, papers_dir, paper_raw_dir,
    ]:
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(settings, "DISCOVERY_DIR", discovery_dir)
    monkeypatch.setattr(settings, "DISCOVERY_MIGRATIONS_DIR", migrations_dir)
    monkeypatch.setattr(settings, "DISCOVERY_LEGACY_ARCHIVE_DIR", legacy_archive_dir)
    monkeypatch.setattr(settings, "DISCOVERY_GENERATIONS_DIR", generations_dir)
    monkeypatch.setattr(settings, "DISCOVERY_STAGING_DIR", staging_dir)
    monkeypatch.setattr(settings, "DISCOVERY_ACTIVE_GENERATION_PATH", active_generation_path)
    monkeypatch.setattr(settings, "DISCOVERY_KEYWORD_NOTEBOOK_DIR", keyword_notebook_dir)
    monkeypatch.setattr(settings, "DISCOVERY_PENDING_PAGES_DIR", pending_pages_dir)
    monkeypatch.setattr(settings, "PAPER_NUMBER_LEDGER_PATH", catalog_dir / "paper_number_ledger.json")
    monkeypatch.setattr(settings, "PAPERS_DIR", papers_dir)
    monkeypatch.setattr(settings, "PAPER_RAW_DIR", paper_raw_dir)

    importlib.reload(workspace_mod)
    importlib.reload(archive_mod)
    importlib.reload(candidate_mod)
    importlib.reload(journal_mod)
    import scripts.migrate_discovery_v4 as migrate_mod
    importlib.reload(migrate_mod)

    return types.SimpleNamespace(
        tmp_path=tmp_path,
        settings=settings,
        migrate_mod=migrate_mod,
        journal_mod=journal_mod,
        workspace_mod=workspace_mod,
    )


def _ready_notebook(keyword_zh: str, *, with_en_query: bool = True) -> dict:
    """Bilingual-ready strict v4 notebook passing validate_notebook + readiness."""
    nb = empty_notebook(keyword_zh)
    nb["enabled"] = True
    zh_q = _empty_search_query(keyword_zh)
    queries = {zh_q["query_id"]: zh_q}
    if with_en_query:
        en_q = _empty_search_query("blowing snow transport")
        queries[en_q["query_id"]] = en_q
    nb["search_queries"] = queries
    nb["relevance_profile"] = _valid_relevance_profile()
    return nb


def _ready_v3_notebook(keyword_zh: str) -> dict:
    """Strict schema-3.0 notebook: valid input for the real v3→v4 migration."""
    nb = _ready_notebook(keyword_zh)
    nb["schema_version"] = "3.0"
    # v3 lane states predate the refresh-window extension fields.
    for entry in nb["search_queries"].values():
        for lanes in entry["providers"].values():
            refresh = lanes["refresh"]
            for key in (
                "last_window_completed_at", "last_window_pages",
                "last_window_signature", "last_window_page_ids",
                "consecutive_failures", "next_retry_at",
            ):
                refresh.pop(key, None)
    return nb


def _write_notebook(notebook_dir: Path, nb: dict) -> Path:
    notebook_dir = Path(notebook_dir)
    notebook_dir.mkdir(parents=True, exist_ok=True)
    path = notebook_dir / f"{nb['keyword_zh']}__{nb['keyword_id'][:8]}.json"
    path.write_text(json.dumps(nb, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _minimal_inventory_report(enabled_keywords: list[str] | None = None) -> dict:
    notebooks = [
        {
            "keyword_zh": kw,
            "enabled": True,
            "active_zh_queries": 1,
            "active_en_queries": 1,
        }
        for kw in (enabled_keywords or [])
    ]
    return {
        "aggregate": {
            "total_journal_files": 0,
            "total_notebook_files": len(notebooks),
            "journal_aggregate_sha256": None,
            "v2_journal_count": 0,
            "v3_journal_count": 0,
            "corrupt_journals": 0,
        },
        "pending_pages": {"total_size_mb": 0, "by_keyword": {}},
        "keyword_notebooks": {"notebooks": notebooks},
    }


def _minimal_archive_result(iso_discovery, mid: str) -> dict:
    """Minimal archive result that still creates the archive layout.

    ``_step_migrate_notebooks`` fails closed when the legacy archive snapshot
    is missing, and the archive step's inventory-binding check reads both
    ``archive_manifest.json`` files, so a mocked archive must create the
    directory layout and (empty) manifests.
    """
    archive_root = iso_discovery.settings.DISCOVERY_LEGACY_ARCHIVE_DIR / mid
    for section in ("pending_pages", "keyword_notebooks"):
        section_dir = archive_root / section
        section_dir.mkdir(parents=True, exist_ok=True)
        (section_dir / "archive_manifest.json").write_text(
            json.dumps({
                "migration_id": mid,
                "source": f"mock-{section}",
                "created_at": "2025-01-01T00:00:00+00:00",
                "total_files": 0,
                "total_size_bytes": 0,
                "aggregate_sha256": None,
                "files": [],
            }),
            encoding="utf-8",
        )
    return {
        "migration_id": mid,
        "archive_root": str(archive_root),
        "pending_pages_total": 0,
        "notebooks_total": 0,
    }


_FAKE_KEYWORDS = ["测试一", "测试二", "测试三"]


def _fake_migrate_all_notebooks(notebook_dir, output_dir):
    """Write minimal bilingual-ready v4 notebooks and report success."""
    results = []
    for keyword_zh in _FAKE_KEYWORDS:
        nb = _ready_notebook(keyword_zh)
        _write_notebook(output_dir, nb)
        results.append({
            "success": True,
            "keyword_zh": keyword_zh,
            "active_queries": 2,
            "lane_count": 8,  # 2 queries × 2 providers × 2 modes
        })
    return results


class TestParser:
    def test_parser_accepts_cutover(self, iso_discovery):
        args = iso_discovery.migrate_mod._parse_args(["--cutover", "mig-123"])
        assert args.cutover == "mig-123"

    def test_parser_accepts_abort(self, iso_discovery):
        args = iso_discovery.migrate_mod._parse_args(["--abort", "mig-123"])
        assert args.abort == "mig-123"

    def test_cutover_and_abort_are_mutually_exclusive(self, iso_discovery):
        with pytest.raises(SystemExit):
            iso_discovery.migrate_mod._parse_args(["--cutover", "a", "--abort", "b"])

    def test_parser_accepts_dir_overrides(self, iso_discovery):
        args = iso_discovery.migrate_mod._parse_args([
            "--apply", "--migrations-dir", "/tmp/mig", "--staging-dir", "/tmp/staging",
        ])
        assert args.migrations_dir == Path("/tmp/mig")
        assert args.staging_dir == Path("/tmp/staging")


class TestApplyAndSmoke:
    def test_smoke_failure_blocks_smoke_passed(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod

        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report", lambda **_: _minimal_inventory_report(_FAKE_KEYWORDS)
        )
        monkeypatch.setattr(
            migrate_mod, "prepare_legacy_archive", lambda mid: _minimal_archive_result(iso_discovery, mid)
        )
        monkeypatch.setattr(migrate_mod, "migrate_all_notebooks", _fake_migrate_all_notebooks)
        monkeypatch.setattr(discover_mod, "main_internal", lambda argv: 1)

        rc = migrate_mod.main([
            "--apply", "--migration-id", "smoke-fail-test",
        ])
        assert rc == 1

        journal = iso_discovery.journal_mod.MigrationJournal.load("smoke-fail-test")
        assert journal.state == iso_discovery.journal_mod.MigrationState.SMOKE_FAILED
        assert not any(
            t["to"] == "smoke_passed" for t in journal.transitions
        )

    def test_smoke_success_reaches_smoke_passed(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod

        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report", lambda **_: _minimal_inventory_report(_FAKE_KEYWORDS)
        )
        monkeypatch.setattr(
            migrate_mod, "prepare_legacy_archive", lambda mid: _minimal_archive_result(iso_discovery, mid)
        )
        monkeypatch.setattr(migrate_mod, "migrate_all_notebooks", _fake_migrate_all_notebooks)
        monkeypatch.setattr(discover_mod, "main_internal", lambda argv: 0)

        rc = migrate_mod.main([
            "--apply", "--migration-id", "smoke-ok-test",
        ])
        assert rc == 0

        journal = iso_discovery.journal_mod.MigrationJournal.load("smoke-ok-test")
        assert journal.state == iso_discovery.journal_mod.MigrationState.SMOKE_PASSED

    def test_apply_then_cutover(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod

        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report", lambda **_: _minimal_inventory_report(_FAKE_KEYWORDS)
        )
        monkeypatch.setattr(
            migrate_mod, "prepare_legacy_archive", lambda mid: _minimal_archive_result(iso_discovery, mid)
        )
        monkeypatch.setattr(migrate_mod, "migrate_all_notebooks", _fake_migrate_all_notebooks)
        monkeypatch.setattr(discover_mod, "main_internal", lambda argv: 0)

        rc = migrate_mod.main([
            "--apply", "--migration-id", "cutover-test",
        ])
        assert rc == 0
        journal = iso_discovery.journal_mod.MigrationJournal.load("cutover-test")
        assert journal.state == iso_discovery.journal_mod.MigrationState.SMOKE_PASSED

        rc = migrate_mod.main(["--cutover", "cutover-test"])
        assert rc == 0
        journal = iso_discovery.journal_mod.MigrationJournal.load("cutover-test")
        assert journal.state == iso_discovery.journal_mod.MigrationState.CUTOVER_COMMITTED
        assert iso_discovery.settings.DISCOVERY_ACTIVE_GENERATION_PATH.is_file()

    def test_apply_then_abort(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod

        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report", lambda **_: _minimal_inventory_report(_FAKE_KEYWORDS)
        )
        monkeypatch.setattr(
            migrate_mod, "prepare_legacy_archive", lambda mid: _minimal_archive_result(iso_discovery, mid)
        )
        monkeypatch.setattr(migrate_mod, "migrate_all_notebooks", _fake_migrate_all_notebooks)
        monkeypatch.setattr(discover_mod, "main_internal", lambda argv: 0)

        rc = migrate_mod.main([
            "--apply", "--migration-id", "abort-test",
        ])
        assert rc == 0

        journal = iso_discovery.journal_mod.MigrationJournal.load("abort-test")
        staging_root = Path(journal.metadata["staging_workspace"])
        assert staging_root.is_dir()

        rc = migrate_mod.main(["--abort", "abort-test"])
        assert rc == 0
        journal = iso_discovery.journal_mod.MigrationJournal.load("abort-test")
        assert journal.state == iso_discovery.journal_mod.MigrationState.ABORTED
        assert not staging_root.exists()


class TestResume:
    def test_resume_from_smoke_failed_retries(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod

        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report", lambda **_: _minimal_inventory_report(_FAKE_KEYWORDS)
        )
        monkeypatch.setattr(
            migrate_mod, "prepare_legacy_archive", lambda mid: _minimal_archive_result(iso_discovery, mid)
        )
        monkeypatch.setattr(migrate_mod, "migrate_all_notebooks", _fake_migrate_all_notebooks)
        monkeypatch.setattr(discover_mod, "main_internal", lambda argv: 1)

        migrate_mod.main([
            "--apply", "--migration-id", "resume-retry-test",
        ])
        journal = journal_mod.MigrationJournal.load("resume-retry-test")
        assert journal.state == journal_mod.MigrationState.SMOKE_FAILED

        monkeypatch.setattr(discover_mod, "main_internal", lambda argv: 0)
        rc = migrate_mod.main(["--resume", "resume-retry-test"])
        assert rc == 0
        journal = journal_mod.MigrationJournal.load("resume-retry-test")
        assert journal.state == journal_mod.MigrationState.SMOKE_PASSED

    def test_resume_from_preflight_skips_completed_steps(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod

        # Build a journal manually at PREFLIGHT_VALIDATED.
        staging_ws = migrate_mod.create_staging_workspace("resume-preflight-test")
        journal = journal_mod.MigrationJournal.create("resume-preflight-test")
        states = [
            journal_mod.MigrationState.INVENTORY_COMPLETE,
            journal_mod.MigrationState.ARCHIVE_PREPARED,
            journal_mod.MigrationState.WORKSPACE_BUILT,
            journal_mod.MigrationState.NOTEBOOKS_STAGED,
            journal_mod.MigrationState.CANDIDATES_EXTRACTED,
            journal_mod.MigrationState.PREFLIGHT_VALIDATED,
        ]
        for s in states:
            journal.transition_to(s)
        journal.metadata["staging_workspace"] = str(staging_ws.root)
        journal.save()

        # Create a minimal notebook so preflight passes on resume.
        _fake_migrate_all_notebooks(None, staging_ws.keyword_notebook_dir)

        monkeypatch.setattr(discover_mod, "main_internal", lambda argv: 0)
        rc = migrate_mod.main(["--resume", "resume-preflight-test"])
        assert rc == 0
        journal = journal_mod.MigrationJournal.load("resume-preflight-test")
        assert journal.state == journal_mod.MigrationState.SMOKE_PASSED


class TestExtractCandidates:
    """Direct coverage of ``_step_extract_candidates`` (strict reader +
    conservation gate + quarantine)."""

    @staticmethod
    def _write_legacy_page(
        pages_dir: Path,
        name: str,
        *,
        keyword_id_value: str,
        keyword_zh: str,
        candidates: list[dict],
        page_id: str = "p" + "0" * 31,
    ) -> Path:
        from tests.helpers.legacy_journals import make_journal

        path = pages_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        page = make_journal(
            candidates,
            keyword_id=keyword_id_value,
            keyword_zh=keyword_zh,
            page_id=page_id,
        )
        path.write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
        return path

    @staticmethod
    def _cand(doi: str, candidate_id: str) -> dict:
        from tests.helpers.legacy_journals import make_candidate

        return make_candidate("pending", doi, candidate_id=candidate_id)

    @staticmethod
    def _write_staged_notebook(staging_ws, keyword_id_value: str, keyword_zh: str) -> None:
        staging_ws.keyword_notebook_dir.mkdir(parents=True, exist_ok=True)
        nb = {
            "schema_version": "4.0",
            "keyword_id": keyword_id_value,
            "keyword_zh": keyword_zh,
            "enabled": True,
            "search_queries": {},
        }
        (staging_ws.keyword_notebook_dir / f"{keyword_zh}__{keyword_id_value[:8]}.json").write_text(
            json.dumps(nb, ensure_ascii=False), encoding="utf-8"
        )

    @staticmethod
    def _journal_at_notebooks_staged(iso_discovery, mid: str):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
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
        return journal, staging_ws

    def test_extract_returns_valid_report_and_pending_files(self, iso_discovery):
        from src.discovery.stores.pending_candidate_store import PendingCandidateStoreV4

        migrate_mod = iso_discovery.migrate_mod
        mid = "extract-ok"
        journal, staging_ws = self._journal_at_notebooks_staged(iso_discovery, mid)
        kid_a, kid_b = "a" * 16, "b" * 16
        self._write_staged_notebook(staging_ws, kid_a, "测试甲")
        self._write_staged_notebook(staging_ws, kid_b, "测试乙")

        pages_dir = iso_discovery.settings.DISCOVERY_DIR / "legacy_archive" / mid / "pending_pages"
        self._write_legacy_page(
            pages_dir, "kw_a/p1.json", keyword_id_value=kid_a, keyword_zh="测试甲",
            candidates=[
                self._cand("10.5555/one", "c1"),
                self._cand("junk", "c9"),
                self._cand("10.5555/one", "c2"),  # duplicate within batch
            ],
            page_id="p" + "1" * 31,
        )
        # Stale legacy keyword_id still resolves through keyword_zh.
        self._write_legacy_page(
            pages_dir, "kw_b/p2.json", keyword_id_value="stale-legacy-id", keyword_zh="测试乙",
            candidates=[self._cand("10.5555/two", "c3")],
            page_id="p" + "2" * 31,
        )
        # Unresolvable keyword attribution is quarantined, never dropped.
        self._write_legacy_page(
            pages_dir, "kw_x/p3.json", keyword_id_value="z" * 16, keyword_zh="不存在",
            candidates=[self._cand("10.5555/three", "c4")],
            page_id="p" + "3" * 31,
        )

        args = migrate_mod._parse_args(["--apply"])
        report = migrate_mod._step_extract_candidates(journal, staging_ws, args)

        assert report.journals_scanned == 3
        assert report.candidates_observed == 5
        assert report.invalid_doi == 1
        assert report.duplicate_seeds == 1
        assert report.already_existing == 0
        assert report.valid_doi_seeds == 3
        assert report.imported == 2
        assert report.terminal == 0
        # The quarantined candidate is counted and unresolved is drained.
        assert report.quarantined == 1
        assert report.unresolved == 0
        assert len(report.errors) == 1
        assert "10.5555/three" in report.errors[0]
        # Conservation: observed == invalid + already_existing + duplicate
        # + imported + terminal + quarantined + unresolved.
        assert report.candidates_observed == (
            report.invalid_doi + report.already_existing + report.duplicate_seeds
            + report.imported + report.terminal + report.quarantined
            + report.unresolved
        )

        # Quarantine evidence file holds the candidate record and reason.
        quarantine = (
            iso_discovery.settings.DISCOVERY_MIGRATIONS_DIR
            / f"{mid}.candidate_quarantine.jsonl"
        )
        assert quarantine.is_file()
        records = [
            json.loads(line)
            for line in quarantine.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(records) == 1
        assert "keyword attribution unresolved" in records[0]["reason"]
        assert records[0]["seed"]["normalized_doi"] == "10.5555/three"
        assert records[0]["seed"]["legacy_page_id"] == "p" + "3" * 31

        journal = iso_discovery.journal_mod.MigrationJournal.load(mid)
        assert journal.state == iso_discovery.journal_mod.MigrationState.CANDIDATES_EXTRACTED
        stats = journal.metadata["candidate_stats"]
        assert stats["quarantined"] == 1
        assert stats["unresolved"] == 0
        assert stats["terminal"] == 0

        store = PendingCandidateStoreV4(staging_ws)
        assert store.count() == 2
        first = store.read(kid_a, "c1")
        assert first is not None
        assert first.origin == "legacy_candidate_seed"
        assert first.normalized_doi == "10.5555/one"
        assert first.keyword_id == kid_a
        assert first.raw_provider_data["provider"] == "openalex"
        second = store.read(kid_b, "c3")
        assert second is not None
        assert second.keyword_id == kid_b
        # No legacy flat seed array may remain.
        assert not (staging_ws.pending_candidates_dir / "legacy_candidate_seeds.json").exists()

    def test_terminal_and_existing_statuses_are_not_imported(self, iso_discovery):
        from tests.helpers.legacy_journals import make_candidate

        migrate_mod = iso_discovery.migrate_mod
        mid = "extract-matrix"
        journal, staging_ws = self._journal_at_notebooks_staged(iso_discovery, mid)
        kid_a = "a" * 16
        self._write_staged_notebook(staging_ws, kid_a, "测试甲")
        pages_dir = iso_discovery.settings.DISCOVERY_DIR / "legacy_archive" / mid / "pending_pages"
        self._write_legacy_page(
            pages_dir, "kw_a/p1.json", keyword_id_value=kid_a, keyword_zh="测试甲",
            candidates=[
                make_candidate("pending", "10.5555/rej", candidate_id="cr",
                               relevance_state="rejected"),
                make_candidate("staged", "10.5555/stg", candidate_id="cs",
                               staged_paper_number="0000000000000001",
                               terminal_reason="staged"),
                make_candidate("existing_duplicate", "10.5555/dup", candidate_id="cd",
                               terminal_reason="doi_duplicate"),
                make_candidate("duplicate_observation", "10.5555/dob", candidate_id="co",
                               terminal_reason="duplicate_observation"),
                make_candidate("unresolved", "", candidate_id="cu",
                               terminal_reason="doi_unresolved"),
                make_candidate("pending", "10.5555/ok", candidate_id="cq",
                               relevance_state="passed"),
            ],
        )

        args = migrate_mod._parse_args(["--apply"])
        report = migrate_mod._step_extract_candidates(journal, staging_ws, args)

        assert report.candidates_observed == 6
        assert report.terminal == 1
        assert report.already_existing == 2
        assert report.duplicate_seeds == 1
        assert report.invalid_doi == 1
        assert report.imported == 1
        assert report.quarantined == 0
        assert report.unresolved == 0

    def test_corrupt_journal_blocks_state_advance(self, iso_discovery):
        migrate_mod = iso_discovery.migrate_mod
        mid = "extract-corrupt"
        journal, staging_ws = self._journal_at_notebooks_staged(iso_discovery, mid)
        kid_a = "a" * 16
        self._write_staged_notebook(staging_ws, kid_a, "测试甲")
        pages_dir = iso_discovery.settings.DISCOVERY_DIR / "legacy_archive" / mid / "pending_pages"
        self._write_legacy_page(
            pages_dir, "kw_a/p1.json", keyword_id_value=kid_a, keyword_zh="测试甲",
            candidates=[self._cand("10.5555/one", "c1")],
        )
        corrupt = pages_dir / "kw_a" / "corrupt.json"
        corrupt.write_text("{not json", encoding="utf-8")

        args = migrate_mod._parse_args(["--apply"])
        with pytest.raises(migrate_mod.MigrationStepError) as excinfo:
            migrate_mod._step_extract_candidates(journal, staging_ws, args)
        assert str(corrupt) in str(excinfo.value)

        journal = iso_discovery.journal_mod.MigrationJournal.load(mid)
        assert journal.state == iso_discovery.journal_mod.MigrationState.NOTEBOOKS_STAGED
        assert "candidate_stats" not in journal.metadata

    def test_unknown_status_blocks_state_advance(self, iso_discovery):
        from tests.helpers.legacy_journals import make_candidate

        migrate_mod = iso_discovery.migrate_mod
        mid = "extract-unknown-status"
        journal, staging_ws = self._journal_at_notebooks_staged(iso_discovery, mid)
        kid_a = "a" * 16
        self._write_staged_notebook(staging_ws, kid_a, "测试甲")
        pages_dir = iso_discovery.settings.DISCOVERY_DIR / "legacy_archive" / mid / "pending_pages"
        self._write_legacy_page(
            pages_dir, "kw_a/p1.json", keyword_id_value=kid_a, keyword_zh="测试甲",
            candidates=[make_candidate("mystery", "10.5555/one", candidate_id="c1")],
        )

        args = migrate_mod._parse_args(["--apply"])
        with pytest.raises(migrate_mod.MigrationStepError):
            migrate_mod._step_extract_candidates(journal, staging_ws, args)
        journal = iso_discovery.journal_mod.MigrationJournal.load(mid)
        assert journal.state == iso_discovery.journal_mod.MigrationState.NOTEBOOKS_STAGED

    def test_quarantine_write_failure_blocks_state_advance(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        mid = "extract-quarantine-fail"
        journal, staging_ws = self._journal_at_notebooks_staged(iso_discovery, mid)
        pages_dir = iso_discovery.settings.DISCOVERY_DIR / "legacy_archive" / mid / "pending_pages"
        self._write_legacy_page(
            pages_dir, "kw_x/p1.json", keyword_id_value="z" * 16, keyword_zh="不存在",
            candidates=[self._cand("10.5555/three", "c4")],
        )

        # The quarantine path's parent is an existing FILE: publishing the
        # streaming quarantine evidence must fail, and the failure must
        # block the journal transition (hard gate).
        blocker = iso_discovery.settings.DISCOVERY_MIGRATIONS_DIR / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        monkeypatch.setattr(
            migrate_mod, "_quarantine_path",
            lambda migration_id: blocker / f"{migration_id}.candidate_quarantine.jsonl",
        )
        args = migrate_mod._parse_args(["--apply"])
        with pytest.raises(OSError):
            migrate_mod._step_extract_candidates(journal, staging_ws, args)
        journal = iso_discovery.journal_mod.MigrationJournal.load(mid)
        assert journal.state == iso_discovery.journal_mod.MigrationState.NOTEBOOKS_STAGED

    def test_conservation_breach_blocks_state_advance(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        mid = "extract-conservation"
        journal, staging_ws = self._journal_at_notebooks_staged(iso_discovery, mid)
        kid_a = "a" * 16
        self._write_staged_notebook(staging_ws, kid_a, "测试甲")
        pages_dir = iso_discovery.settings.DISCOVERY_DIR / "legacy_archive" / mid / "pending_pages"
        self._write_legacy_page(
            pages_dir, "kw_a/p1.json", keyword_id_value=kid_a, keyword_zh="测试甲",
            candidates=[self._cand("10.5555/one", "c1")],
        )

        original = migrate_mod.assert_conservation

        def poisoned(report):
            report.candidates_observed += 1  # break the equation
            original(report)

        monkeypatch.setattr(migrate_mod, "assert_conservation", poisoned)
        args = migrate_mod._parse_args(["--apply"])
        with pytest.raises(migrate_mod.MigrationStepError, match="conservation"):
            migrate_mod._step_extract_candidates(journal, staging_ws, args)
        journal = iso_discovery.journal_mod.MigrationJournal.load(mid)
        assert journal.state == iso_discovery.journal_mod.MigrationState.NOTEBOOKS_STAGED

    def test_write_failure_does_not_advance_journal_state(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        mid = "extract-fail"
        journal, staging_ws = self._journal_at_notebooks_staged(iso_discovery, mid)
        kid_a = "a" * 16
        self._write_staged_notebook(staging_ws, kid_a, "测试甲")
        pages_dir = iso_discovery.settings.DISCOVERY_DIR / "legacy_archive" / mid / "pending_pages"
        self._write_legacy_page(
            pages_dir, "kw_a/p1.json", keyword_id_value=kid_a, keyword_zh="测试甲",
            candidates=[self._cand("10.5555/one", "c1")],
        )

        class ExplodingStore:
            def __init__(self, workspace):
                pass

            def write(self, candidate):
                raise RuntimeError("disk full")

            def count(self):
                return 0

        monkeypatch.setattr(migrate_mod, "PendingCandidateStoreV4", ExplodingStore)
        args = migrate_mod._parse_args(["--apply"])
        with pytest.raises(RuntimeError, match="disk full"):
            migrate_mod._step_extract_candidates(journal, staging_ws, args)

        journal = iso_discovery.journal_mod.MigrationJournal.load(mid)
        assert journal.state == iso_discovery.journal_mod.MigrationState.NOTEBOOKS_STAGED
        assert "candidate_stats" not in journal.metadata

    def test_auto_skip_only_when_inventory_proves_zero_eligible(
        self, iso_discovery, monkeypatch
    ):
        """eligible_legacy_candidates == 0 auto-skips; anything else extracts."""
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod

        def fake_preflight(j, ws, nb_results, a):
            j.transition_to(journal_mod.MigrationState.PREFLIGHT_VALIDATED)
            j.save()

        def fake_smoke(j, ws, a):
            j.transition_to(journal_mod.MigrationState.SMOKE_PASSED)
            j.save()
            return 0

        monkeypatch.setattr(migrate_mod, "_step_preflight", fake_preflight)
        monkeypatch.setattr(migrate_mod, "_step_smoke", fake_smoke)

        def forbidden_extract(*fargs, **fkwargs):
            pytest.fail("extraction must be auto-skipped when eligible == 0")

        # Zero eligible: the step is skipped with a recorded reason.
        mid_zero = "extract-auto-skip"
        journal, staging_ws = self._journal_at_notebooks_staged(iso_discovery, mid_zero)
        journal.metadata["eligible_legacy_candidates"] = 0
        journal.save()
        monkeypatch.setattr(migrate_mod, "_step_extract_candidates", forbidden_extract)
        args = migrate_mod._parse_args(["--apply"])
        rc = migrate_mod._run_apply_from_state(journal, args)
        assert rc == 0
        journal = journal_mod.MigrationJournal.load(mid_zero)
        assert journal.state == journal_mod.MigrationState.SMOKE_PASSED
        assert not any(
            t["from"] == "notebooks_staged" and t["to"] != "candidates_extracted"
            for t in journal.transitions
        )

        # Positive eligible: the extraction step runs.
        mid_pos = "extract-auto-run"
        journal, staging_ws = self._journal_at_notebooks_staged(iso_discovery, mid_pos)
        journal.metadata["eligible_legacy_candidates"] = 7
        journal.save()
        calls: dict[str, int] = {}

        def recording_extract(j, ws, a):
            calls["n"] = calls.get("n", 0) + 1
            j.transition_to(journal_mod.MigrationState.CANDIDATES_EXTRACTED)
            j.save()

        monkeypatch.setattr(migrate_mod, "_step_extract_candidates", recording_extract)
        rc = migrate_mod._run_apply_from_state(journal, args)
        assert rc == 0
        assert calls["n"] == 1

    def test_inventory_records_eligible_legacy_candidates(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod

        report = _minimal_inventory_report(_FAKE_KEYWORDS)
        report["pending_pages"]["files"] = [
            {"candidate_count": 3},
            {"candidate_count": 4},
            {"candidate_count": 99, "error": "json_decode_error:x"},
        ]
        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report", lambda **_: report,
        )
        journal = journal_mod.MigrationJournal.create("extract-eligible")
        args = migrate_mod._parse_args(["--apply"])
        migrate_mod._step_inventory(journal, args)
        assert journal.metadata["eligible_legacy_candidates"] == 7

    def test_resume_from_candidates_extracted_does_not_replay(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "extract-resume"
        journal, staging_ws = self._journal_at_notebooks_staged(iso_discovery, mid)
        journal.transition_to(journal_mod.MigrationState.CANDIDATES_EXTRACTED)
        journal.metadata["notebooks_failed"] = 0
        journal.metadata["inventory_enabled_notebook_count"] = 1
        journal.metadata["inventory_enabled_keyword_zh"] = ["测试甲"]
        journal.metadata["candidate_stats"] = _zero_candidate_stats()
        journal.save()
        _write_notebook(staging_ws.keyword_notebook_dir, _ready_notebook("测试甲"))

        def forbidden(*fargs, **fkwargs):
            pytest.fail("resume must not replay candidate extraction")

        monkeypatch.setattr(migrate_mod, "stream_extract_candidates", forbidden)
        monkeypatch.setattr(discover_mod, "main_internal", lambda argv: 0)

        rc = migrate_mod.main(["--resume", mid])
        assert rc == 0
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.SMOKE_PASSED


class TestDryRun:
    def test_dry_run_does_not_create_journal_or_staging(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod

        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report", lambda **_: _minimal_inventory_report(_FAKE_KEYWORDS)
        )
        monkeypatch.setattr(migrate_mod, "migrate_all_notebooks", lambda notebook_dir, output_dir: [])

        rc = migrate_mod.main(["--dry-run", "--migration-id", "dryrun-test"])
        assert rc == 0

        # No journal should be created.
        journal_path = iso_discovery.settings.DISCOVERY_MIGRATIONS_DIR / "dryrun-test.json"
        assert not journal_path.exists()

        # No staging workspace under the real staging dir should be created.
        assert not any(iso_discovery.settings.DISCOVERY_STAGING_DIR.iterdir())

        # Dry-run is write-only-to-stdout: no plan file may be written.
        plan_path = iso_discovery.settings.DISCOVERY_DIR / "migrations" / "v4_migration_plan.json"
        assert not plan_path.exists()

    def test_dry_run_validates_notebook_migration(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod

        calls = []

        def fake_migrate(notebook_dir, output_dir):
            calls.append(output_dir)
            return [
                {"success": True, "keyword_zh": "测试", "active_queries": 1, "lane_count": 4},
            ]

        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report", lambda **_: _minimal_inventory_report(_FAKE_KEYWORDS)
        )
        monkeypatch.setattr(migrate_mod, "migrate_all_notebooks", fake_migrate)

        rc = migrate_mod.main(["--dry-run"])
        assert rc == 0
        assert len(calls) == 1
        # Output dir must be a temporary directory, not under DISCOVERY_STAGING_DIR.
        assert Path(calls[0]).resolve() != iso_discovery.settings.DISCOVERY_STAGING_DIR.resolve()


class TestCutoverAbortValidation:
    def test_cutover_rejects_preflight_without_skip_flag(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod

        journal = journal_mod.MigrationJournal.create("cutover-reject-test")
        for s in [
            journal_mod.MigrationState.INVENTORY_COMPLETE,
            journal_mod.MigrationState.ARCHIVE_PREPARED,
            journal_mod.MigrationState.WORKSPACE_BUILT,
            journal_mod.MigrationState.NOTEBOOKS_STAGED,
            journal_mod.MigrationState.CANDIDATES_EXTRACTED,
            journal_mod.MigrationState.PREFLIGHT_VALIDATED,
        ]:
            journal.transition_to(s)
        journal.save()

        rc = migrate_mod.main(["--cutover", "cutover-reject-test"])
        assert rc == 1

    def test_abort_rejects_terminal_states(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod

        for state_name in ["cutover_committed", "legacy_cleaned", "finalized", "aborted"]:
            journal = journal_mod.MigrationJournal.create(f"abort-{state_name}-test")
            state = journal_mod.MigrationState(state_name)
            for s in [
                journal_mod.MigrationState.INVENTORY_COMPLETE,
                journal_mod.MigrationState.ARCHIVE_PREPARED,
                journal_mod.MigrationState.WORKSPACE_BUILT,
                journal_mod.MigrationState.NOTEBOOKS_STAGED,
                journal_mod.MigrationState.CANDIDATES_EXTRACTED,
                journal_mod.MigrationState.PREFLIGHT_VALIDATED,
                journal_mod.MigrationState.SMOKE_PASSED,
                journal_mod.MigrationState.CUTOVER_COMMITTED,
                journal_mod.MigrationState.LEGACY_CLEANED,
                journal_mod.MigrationState.FINALIZED,
            ]:
                journal.transition_to(s)
                if journal.state == state:
                    break
            journal.save()
            rc = migrate_mod.main(["--abort", f"abort-{state_name}-test"])
            assert rc == 1, f"abort from {state_name} should be rejected"


def _zero_candidate_stats() -> dict:
    """Candidate stats block for journals driven past extraction by hand."""
    return {
        "journals_scanned": 0,
        "candidates_observed": 0,
        "valid_doi_seeds": 0,
        "invalid_doi": 0,
        "already_existing": 0,
        "duplicate_seeds": 0,
        "imported": 0,
        "terminal": 0,
        "quarantined": 0,
        "unresolved": 0,
        "errors": [],
    }


def _journal_at_candidates_extracted(iso_discovery, mid: str, *, expected_zh: list[str]):
    """Journal at CANDIDATES_EXTRACTED with inventory expectations recorded."""
    migrate_mod = iso_discovery.migrate_mod
    journal_mod = iso_discovery.journal_mod
    staging_ws = migrate_mod.create_staging_workspace(mid)
    journal = journal_mod.MigrationJournal.create(mid)
    for s in [
        journal_mod.MigrationState.INVENTORY_COMPLETE,
        journal_mod.MigrationState.ARCHIVE_PREPARED,
        journal_mod.MigrationState.WORKSPACE_BUILT,
        journal_mod.MigrationState.NOTEBOOKS_STAGED,
        journal_mod.MigrationState.CANDIDATES_EXTRACTED,
    ]:
        journal.transition_to(s)
    journal.metadata["staging_workspace"] = str(staging_ws.root)
    journal.metadata["notebooks_failed"] = 0
    journal.metadata["inventory_enabled_notebook_count"] = len(expected_zh)
    journal.metadata["inventory_enabled_keyword_zh"] = sorted(expected_zh)
    journal.metadata["candidate_stats"] = _zero_candidate_stats()
    journal.save()
    return journal, staging_ws


def _apply_args(migrate_mod):
    return migrate_mod._parse_args(["--apply"])


class TestNotebookMigrationGate:
    """A3: notebook migration failures must block NOTEBOOKS_STAGED."""

    def test_partial_notebook_failure_blocks_state_advance(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod

        def fake_partial(notebook_dir, output_dir):
            _write_notebook(output_dir, _ready_notebook("测试一"))
            return [
                {"success": True, "keyword_zh": "测试一", "active_queries": 2, "lane_count": 8},
                {"success": False, "keyword_zh": "测试二", "error": "not discovery-ready"},
            ]

        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report",
            lambda **_: _minimal_inventory_report(["测试一", "测试二"]),
        )
        monkeypatch.setattr(
            migrate_mod, "prepare_legacy_archive", lambda mid: _minimal_archive_result(iso_discovery, mid)
        )
        monkeypatch.setattr(migrate_mod, "migrate_all_notebooks", fake_partial)
        monkeypatch.setattr(discover_mod, "main_internal", lambda argv: 0)

        rc = migrate_mod.main([
            "--apply", "--migration-id", "nb-fail-test",
        ])
        assert rc == 1

        journal = journal_mod.MigrationJournal.load("nb-fail-test")
        assert journal.state == journal_mod.MigrationState.WORKSPACE_BUILT
        assert not any(t["to"] == "notebooks_staged" for t in journal.transitions)

    def test_success_count_mismatch_blocks_state_advance(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod

        def fake_short(notebook_dir, output_dir):
            _write_notebook(output_dir, _ready_notebook("测试一"))
            return [
                {"success": True, "keyword_zh": "测试一", "active_queries": 2, "lane_count": 8},
            ]

        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report",
            lambda **_: _minimal_inventory_report(["测试一", "测试二"]),
        )
        monkeypatch.setattr(
            migrate_mod, "prepare_legacy_archive", lambda mid: _minimal_archive_result(iso_discovery, mid)
        )
        monkeypatch.setattr(migrate_mod, "migrate_all_notebooks", fake_short)
        monkeypatch.setattr(discover_mod, "main_internal", lambda argv: 0)

        rc = migrate_mod.main([
            "--apply", "--migration-id", "nb-count-test",
        ])
        assert rc == 1

        journal = journal_mod.MigrationJournal.load("nb-count-test")
        assert journal.state == journal_mod.MigrationState.WORKSPACE_BUILT
        assert not any(t["to"] == "notebooks_staged" for t in journal.transitions)


class TestPreflightGate:
    """A3: preflight hard gates (P0-3)."""

    def test_empty_workspace_rejected(self, iso_discovery):
        migrate_mod = iso_discovery.migrate_mod
        journal, staging_ws = _journal_at_candidates_extracted(
            iso_discovery, "pf-empty-test", expected_zh=["测试甲"],
        )
        with pytest.raises(migrate_mod.MigrationStepError):
            migrate_mod._step_preflight(journal, staging_ws, [], _apply_args(migrate_mod))
        journal = iso_discovery.journal_mod.MigrationJournal.load("pf-empty-test")
        assert journal.state == iso_discovery.journal_mod.MigrationState.CANDIDATES_EXTRACTED

    def test_keyword_set_mismatch_rejected(self, iso_discovery):
        migrate_mod = iso_discovery.migrate_mod
        journal, staging_ws = _journal_at_candidates_extracted(
            iso_discovery, "pf-kw-test", expected_zh=["测试乙"],
        )
        _write_notebook(staging_ws.keyword_notebook_dir, _ready_notebook("测试甲"))
        nb_results = [{"success": True, "keyword_zh": "测试甲", "lane_count": 8}]
        with pytest.raises(migrate_mod.MigrationStepError):
            migrate_mod._step_preflight(
                journal, staging_ws, nb_results, _apply_args(migrate_mod),
            )
        journal = iso_discovery.journal_mod.MigrationJournal.load("pf-kw-test")
        assert journal.state == iso_discovery.journal_mod.MigrationState.CANDIDATES_EXTRACTED

    def test_zero_lanes_rejected(self, iso_discovery):
        migrate_mod = iso_discovery.migrate_mod
        journal, staging_ws = _journal_at_candidates_extracted(
            iso_discovery, "pf-lane-test", expected_zh=["测试甲"],
        )
        _write_notebook(staging_ws.keyword_notebook_dir, _ready_notebook("测试甲"))
        nb_results = [{"success": True, "keyword_zh": "测试甲", "lane_count": 0}]
        with pytest.raises(migrate_mod.MigrationStepError):
            migrate_mod._step_preflight(
                journal, staging_ws, nb_results, _apply_args(migrate_mod),
            )
        journal = iso_discovery.journal_mod.MigrationJournal.load("pf-lane-test")
        assert journal.state == iso_discovery.journal_mod.MigrationState.CANDIDATES_EXTRACTED

    def test_bilingual_incomplete_rejected(self, iso_discovery):
        migrate_mod = iso_discovery.migrate_mod
        journal, staging_ws = _journal_at_candidates_extracted(
            iso_discovery, "pf-bilingual-test", expected_zh=["测试甲"],
        )
        _write_notebook(
            staging_ws.keyword_notebook_dir,
            _ready_notebook("测试甲", with_en_query=False),
        )
        nb_results = [{"success": True, "keyword_zh": "测试甲", "lane_count": 4}]
        with pytest.raises(migrate_mod.MigrationStepError):
            migrate_mod._step_preflight(
                journal, staging_ws, nb_results, _apply_args(migrate_mod),
            )
        journal = iso_discovery.journal_mod.MigrationJournal.load("pf-bilingual-test")
        assert journal.state == iso_discovery.journal_mod.MigrationState.CANDIDATES_EXTRACTED

    def test_notebooks_failed_metadata_rejected(self, iso_discovery):
        migrate_mod = iso_discovery.migrate_mod
        journal, staging_ws = _journal_at_candidates_extracted(
            iso_discovery, "pf-meta-test", expected_zh=["测试甲"],
        )
        journal.metadata["notebooks_failed"] = 1
        journal.save()
        _write_notebook(staging_ws.keyword_notebook_dir, _ready_notebook("测试甲"))
        nb_results = [{"success": True, "keyword_zh": "测试甲", "lane_count": 8}]
        with pytest.raises(migrate_mod.MigrationStepError):
            migrate_mod._step_preflight(
                journal, staging_ws, nb_results, _apply_args(migrate_mod),
            )

    def test_valid_workspace_passes(self, iso_discovery):
        migrate_mod = iso_discovery.migrate_mod
        journal, staging_ws = _journal_at_candidates_extracted(
            iso_discovery, "pf-ok-test", expected_zh=["测试甲"],
        )
        _write_notebook(staging_ws.keyword_notebook_dir, _ready_notebook("测试甲"))
        nb_results = [{"success": True, "keyword_zh": "测试甲", "lane_count": 8}]
        migrate_mod._step_preflight(journal, staging_ws, nb_results, _apply_args(migrate_mod))
        journal = iso_discovery.journal_mod.MigrationJournal.load("pf-ok-test")
        assert journal.state == iso_discovery.journal_mod.MigrationState.PREFLIGHT_VALIDATED
        assert journal.metadata["total_lanes"] == 8


class TestSkipRealSmoke:
    """A4: --skip-real-smoke must never grant cutover eligibility (P0-4)."""

    @staticmethod
    def _apply_with_skip(iso_discovery, monkeypatch, mid: str):
        migrate_mod = iso_discovery.migrate_mod
        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report",
            lambda **_: _minimal_inventory_report(_FAKE_KEYWORDS),
        )
        monkeypatch.setattr(
            migrate_mod, "prepare_legacy_archive", lambda m: _minimal_archive_result(iso_discovery, m)
        )
        monkeypatch.setattr(migrate_mod, "migrate_all_notebooks", _fake_migrate_all_notebooks)
        rc = migrate_mod.main([
            "--apply", "--migration-id", mid,
            "--skip-real-smoke",
        ])
        return rc

    def test_skip_smoke_stays_preflight_validated(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod

        def forbidden_smoke(argv):
            pytest.fail("smoke must not run under --skip-real-smoke")

        monkeypatch.setattr(discover_mod, "main_internal", forbidden_smoke)
        rc = self._apply_with_skip(iso_discovery, monkeypatch, "skip-smoke-test")
        assert rc == 0

        journal = journal_mod.MigrationJournal.load("skip-smoke-test")
        assert journal.state == journal_mod.MigrationState.PREFLIGHT_VALIDATED
        assert journal.metadata.get("smoke_skipped") is True
        assert not any(t["to"] == "smoke_passed" for t in journal.transitions)

    def test_cutover_rejected_after_skipped_smoke(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        monkeypatch.setattr(discover_mod, "main_internal", lambda argv: 0)
        rc = self._apply_with_skip(iso_discovery, monkeypatch, "skip-cutover-test")
        assert rc == 0

        # Both with and without the flag, cutover from preflight is refused.
        assert migrate_mod.main(["--cutover", "skip-cutover-test"]) == 1
        assert migrate_mod.main(["--cutover", "skip-cutover-test", "--skip-real-smoke"]) == 1
        journal = journal_mod.MigrationJournal.load("skip-cutover-test")
        assert journal.state == journal_mod.MigrationState.PREFLIGHT_VALIDATED

    def test_cutover_rejects_skip_flag_even_when_smoke_passed(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report",
            lambda **_: _minimal_inventory_report(_FAKE_KEYWORDS),
        )
        monkeypatch.setattr(
            migrate_mod, "prepare_legacy_archive", lambda m: _minimal_archive_result(iso_discovery, m)
        )
        monkeypatch.setattr(migrate_mod, "migrate_all_notebooks", _fake_migrate_all_notebooks)
        monkeypatch.setattr(discover_mod, "main_internal", lambda argv: 0)

        rc = migrate_mod.main([
            "--apply", "--migration-id", "skip-flag-cutover-test",
        ])
        assert rc == 0
        journal = journal_mod.MigrationJournal.load("skip-flag-cutover-test")
        assert journal.state == journal_mod.MigrationState.SMOKE_PASSED

        assert migrate_mod.main(["--cutover", "skip-flag-cutover-test", "--skip-real-smoke"]) == 1
        assert migrate_mod.main(["--cutover", "skip-flag-cutover-test"]) == 0


class TestSmokeIsolation:
    """B1/P0-2: smoke must run against an ephemeral clone, never the staging workspace."""

    def test_smoke_args_use_staging_isolated_paths(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod

        captured: dict[str, list[str]] = {}

        def fake_main(argv):
            captured["argv"] = list(argv)
            return 0

        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report",
            lambda **_: _minimal_inventory_report(_FAKE_KEYWORDS),
        )
        monkeypatch.setattr(
            migrate_mod, "prepare_legacy_archive", lambda m: _minimal_archive_result(iso_discovery, m)
        )
        monkeypatch.setattr(migrate_mod, "migrate_all_notebooks", _fake_migrate_all_notebooks)
        monkeypatch.setattr(discover_mod, "main_internal", fake_main)

        rc = migrate_mod.main([
            "--apply", "--migration-id", "smoke-iso-test",
        ])
        assert rc == 0

        argv = captured["argv"]

        def arg_value(flag: str) -> Path:
            return Path(argv[argv.index(flag) + 1])

        journal = journal_mod.MigrationJournal.load("smoke-iso-test")
        staging_root = Path(journal.metadata["staging_workspace"])
        workspace_root = arg_value("--workspace-root")
        paper_raw = arg_value("--paper-raw-dir")
        papers = arg_value("--papers-dir")
        ledger = arg_value("--ledger-path")
        report_dir = arg_value("--report-dir")
        output_dir = arg_value("--output-dir")
        # The smoke run targets an ephemeral clone, never the real staging
        # workspace, and the clone is removed once the run finishes.
        assert workspace_root != staging_root
        assert not workspace_root.exists()
        smoke_root = workspace_root / "smoke"
        assert paper_raw == smoke_root / "paper_raw"
        assert papers == smoke_root / "papers"
        assert ledger == smoke_root / "paper_number_ledger.json"
        assert report_dir == smoke_root / "reports"
        assert output_dir == smoke_root / "exports"
        # Never the configured production targets.
        assert paper_raw != iso_discovery.settings.PAPER_RAW_DIR
        assert papers != iso_discovery.settings.PAPERS_DIR
        assert ledger != iso_discovery.settings.PAPER_NUMBER_LEDGER_PATH
        # Isolation is recorded in the journal for audit.
        assert journal.metadata["smoke_isolation"] == {
            "ephemeral_clone": True,
            "workspace_root": str(workspace_root),
            "paper_raw_dir": str(paper_raw),
            "papers_dir": str(papers),
            "ledger_path": str(ledger),
            "report_dir": str(report_dir),
            "output_dir": str(output_dir),
        }


class TestSmokeZeroSideEffects:
    """P0-2: the smoke run must leave the real staging workspace byte-identical."""

    N_CANDIDATES = 5

    @staticmethod
    def _seed_pending_candidates(staging_ws, keyword_id: str, count: int) -> dict[str, bytes]:
        """Write ``count`` pending candidates; return {path: original bytes}."""
        store = PendingCandidateStoreV4(staging_ws)
        payloads: dict[str, bytes] = {}
        for i in range(count):
            candidate = PendingCandidateV4(
                candidate_id=f"cand-{i:03d}",
                keyword_id=keyword_id,
                origin="legacy_candidate_seed",
                doi=f"10.0000/test.{i:03d}",
                normalized_doi=f"10.0000/test.{i:03d}",
                title=f"Candidate {i}",
                created_at="2026-01-01T00:00:00+00:00",
            )
            path = store.write(candidate)
            payloads[str(path)] = path.read_bytes()
        return payloads

    def _apply_with_seeded_candidates(self, iso_discovery, monkeypatch, mid, smoke_impl):
        """Drive --apply with candidates seeded into staging before the smoke step."""
        migrate_mod = iso_discovery.migrate_mod
        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report",
            lambda **_: _minimal_inventory_report(_FAKE_KEYWORDS),
        )
        monkeypatch.setattr(
            migrate_mod, "prepare_legacy_archive", lambda m: _minimal_archive_result(iso_discovery, m)
        )
        monkeypatch.setattr(migrate_mod, "migrate_all_notebooks", _fake_migrate_all_notebooks)

        seeded: dict[str, object] = {}
        original_preflight = migrate_mod._step_preflight

        def preflight_then_seed(journal, staging_ws, nb_results, args):
            original_preflight(journal, staging_ws, nb_results, args)
            nb_path = sorted(staging_ws.keyword_notebook_dir.glob("*.json"))[0]
            keyword_id = json.loads(nb_path.read_text(encoding="utf-8"))["keyword_id"]
            seeded["workspace"] = staging_ws
            seeded["payloads"] = self._seed_pending_candidates(
                staging_ws, keyword_id, self.N_CANDIDATES
            )

        monkeypatch.setattr(migrate_mod, "_step_preflight", preflight_then_seed)
        monkeypatch.setattr(discover_mod, "main_internal", smoke_impl)

        rc = migrate_mod.main([
            "--apply", "--migration-id", mid,
        ])
        return rc, seeded

    def test_smoke_leaves_staging_tree_and_candidates_untouched(self, iso_discovery, monkeypatch):
        journal_mod = iso_discovery.journal_mod

        def draining_smoke(argv):
            # Simulate a real drain against the workspace it was given:
            # consume (delete) pending candidates and write lane state.
            workspace_root = Path(argv[argv.index("--workspace-root") + 1])
            pending_dir = workspace_root / "pending_candidates"
            for path in pending_dir.rglob("*.json"):
                path.unlink()
            (workspace_root / "lane_states" / "smoke_lane.json").write_text(
                "{}", encoding="utf-8"
            )
            return 0

        rc, seeded = self._apply_with_seeded_candidates(
            iso_discovery, monkeypatch, "smoke-pure-test", draining_smoke
        )
        assert rc == 0
        journal = journal_mod.MigrationJournal.load("smoke-pure-test")
        assert journal.state == journal_mod.MigrationState.SMOKE_PASSED

        # The drain consumed only the clone: the real staging workspace still
        # holds every seeded candidate with byte-identical content.
        staging_ws = seeded["workspace"]
        store = PendingCandidateStoreV4(staging_ws)
        assert store.count() == self.N_CANDIDATES
        for path_str, payload in seeded["payloads"].items():
            path = Path(path_str)
            assert path.is_file()
            assert path.read_bytes() == payload

        # Tree-hash equality evidence is recorded in the journal.
        evidence = journal.metadata["smoke_tree_hash"]
        assert evidence["equal"] is True
        assert evidence["before"] == evidence["after"]
        isolation = journal.metadata["smoke_isolation"]
        assert isolation["ephemeral_clone"] is True
        clone_root = Path(isolation["workspace_root"])
        assert clone_root != staging_ws.root
        assert not clone_root.exists()

    def test_smoke_fails_when_staging_workspace_is_dirtied(self, iso_discovery, monkeypatch):
        journal_mod = iso_discovery.journal_mod

        def dirtying_smoke(argv):
            # Dirty the REAL staging workspace: the clone root name is the
            # generation id, so the real staging root is recoverable.
            clone_root = Path(argv[argv.index("--workspace-root") + 1])
            staging_root = iso_discovery.settings.DISCOVERY_STAGING_DIR / clone_root.name
            (staging_root / "lane_states" / "dirty.json").write_text(
                "{}", encoding="utf-8"
            )
            return 0

        rc, seeded = self._apply_with_seeded_candidates(
            iso_discovery, monkeypatch, "smoke-dirty-test", dirtying_smoke
        )
        assert rc == 1
        journal = journal_mod.MigrationJournal.load("smoke-dirty-test")
        assert journal.state == journal_mod.MigrationState.SMOKE_FAILED
        assert journal.metadata["smoke_tree_hash"]["equal"] is False
        assert not any(t["to"] == "smoke_passed" for t in journal.transitions)

        # The failed smoke never consumed the seeded candidates either.
        staging_ws = seeded["workspace"]
        store = PendingCandidateStoreV4(staging_ws)
        assert store.count() == self.N_CANDIDATES


# ── A5/A6 helpers ─────────────────────────────────────────────────────────


def _inventory_report_with_real_pages(iso_discovery, keywords: list[str]) -> dict:
    """Mock inventory whose pending_pages entries are the real on-disk files.

    The mocked notebook section keeps the fake enabled-keyword expectations;
    the pending_pages section carries real sha256/size entries so the
    inventory↔archive binding closure is consistent with a real archive.
    """
    from src.migrations.discovery_v4 import legacy_inventory as _legacy_inventory

    report = _minimal_inventory_report(keywords)
    real = _legacy_inventory.generate_inventory_report(
        pending_pages_dir=iso_discovery.settings.DISCOVERY_PENDING_PAGES_DIR,
        notebooks_dir=iso_discovery.settings.DISCOVERY_KEYWORD_NOTEBOOK_DIR,
    )
    report["pending_pages"]["files"] = real["pending_pages"]["files"]
    return report


def _apply_to_smoke_passed(iso_discovery, monkeypatch, mid: str, *, archive_impl=None,
                           inventory_report=None):
    """Drive a migration to SMOKE_PASSED with fully mocked external steps."""
    migrate_mod = iso_discovery.migrate_mod
    monkeypatch.setattr(
        migrate_mod, "generate_inventory_report",
        lambda **_: inventory_report or _minimal_inventory_report(_FAKE_KEYWORDS),
    )
    monkeypatch.setattr(
        migrate_mod, "prepare_legacy_archive",
        archive_impl or (lambda m: _minimal_archive_result(iso_discovery, m)),
    )
    monkeypatch.setattr(migrate_mod, "migrate_all_notebooks", _fake_migrate_all_notebooks)
    monkeypatch.setattr(discover_mod, "main_internal", lambda argv: 0)
    rc = migrate_mod.main([
        "--apply", "--migration-id", mid,
    ])
    assert rc == 0
    journal = iso_discovery.journal_mod.MigrationJournal.load(mid)
    assert journal.state == iso_discovery.journal_mod.MigrationState.SMOKE_PASSED


def _real_archive_impl(settings):
    """Build a real legacy archive (valid manifests) in the isolated dirs."""
    from src.migrations.discovery_v4 import archive_builder as archive_builder_mod

    def _impl(mid: str) -> dict:
        archive_root = settings.DISCOVERY_LEGACY_ARCHIVE_DIR / mid
        archive_root.mkdir(parents=True, exist_ok=True)
        pages = archive_builder_mod.archive_pending_pages(
            settings.DISCOVERY_PENDING_PAGES_DIR, archive_root, mid,
        )
        notebooks = archive_builder_mod.archive_keyword_notebooks(
            settings.DISCOVERY_KEYWORD_NOTEBOOK_DIR, archive_root, mid,
        )
        return {
            "migration_id": mid,
            "archive_root": str(archive_root),
            "pending_pages_total": pages["total_files"],
            "notebooks_total": notebooks["total_files"],
        }

    return _impl


def _seed_old_active_pointer(iso_discovery, generation_id: str = "v4-old-gen"):
    """Write a strict pre-existing active pointer (a previous generation)."""
    settings = iso_discovery.settings
    old = ActiveGenerationPointerV4(
        generation_id=generation_id,
        workspace_manifest_sha256="0" * 64,
        activated_at="2025-01-01T00:00:00+00:00",
        migration_id="v4-old-migration",
    )
    settings.DISCOVERY_ACTIVE_GENERATION_PATH.write_text(
        json.dumps(old.to_dict()), encoding="utf-8"
    )
    return old


def _read_active_pointer_dict(iso_discovery) -> dict:
    return json.loads(
        iso_discovery.settings.DISCOVERY_ACTIVE_GENERATION_PATH.read_text(encoding="utf-8")
    )


class TestCutoverReconciliation:
    """A5: cutover self-heals from every crash window and stays idempotent."""

    def test_crash_after_rename_self_heals(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "cut-crash-rename"
        _apply_to_smoke_passed(iso_discovery, monkeypatch, mid)

        # Simulate a crash after the staging rename but before pointer write:
        # workspace.json was already built into staging by cmd_cutover.
        journal = journal_mod.MigrationJournal.load(mid)
        staging_ws = migrate_mod._resolve_staging_ws_from_journal(journal)
        migrate_mod._build_workspace_manifest(staging_ws, mid, None, 24)
        target = iso_discovery.settings.DISCOVERY_GENERATIONS_DIR / mid
        os.rename(str(staging_ws.root), str(target))
        assert not iso_discovery.settings.DISCOVERY_ACTIVE_GENERATION_PATH.exists()

        rc = migrate_mod.main(["--cutover", mid])
        assert rc == 0
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.CUTOVER_COMMITTED
        pointer = _read_active_pointer_dict(iso_discovery)
        assert pointer["generation_id"] == mid

        # Rerun is fully idempotent.
        assert migrate_mod.main(["--cutover", mid]) == 0
        assert _read_active_pointer_dict(iso_discovery) == pointer

    def test_crash_after_pointer_write_self_heals(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "cut-crash-journal"
        _apply_to_smoke_passed(iso_discovery, monkeypatch, mid)

        # Simulate a crash after the pointer write but before journal save.
        original_save = migrate_mod.MigrationJournal.save
        calls = {"n": 0}

        def flaky_save(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated crash before journal save")
            return original_save(self)

        monkeypatch.setattr(migrate_mod.MigrationJournal, "save", flaky_save)
        with pytest.raises(RuntimeError, match="simulated crash"):
            migrate_mod.main(["--cutover", mid])

        # Journal never advanced; the filesystem commit is complete.
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.SMOKE_PASSED
        assert iso_discovery.settings.DISCOVERY_ACTIVE_GENERATION_PATH.is_file()

        rc = migrate_mod.main(["--cutover", mid])
        assert rc == 0
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.CUTOVER_COMMITTED
        pointer = _read_active_pointer_dict(iso_discovery)
        assert pointer["generation_id"] == mid

        # Repeat: already committed, still succeeds and changes nothing.
        assert migrate_mod.main(["--cutover", mid]) == 0
        assert _read_active_pointer_dict(iso_discovery) == pointer

    def test_cutover_idempotent_double_run(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "cut-idem"
        _apply_to_smoke_passed(iso_discovery, monkeypatch, mid)

        assert migrate_mod.main(["--cutover", mid]) == 0
        pointer = _read_active_pointer_dict(iso_discovery)
        assert migrate_mod.main(["--cutover", mid]) == 0
        assert _read_active_pointer_dict(iso_discovery) == pointer
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.CUTOVER_COMMITTED

    def test_cutover_records_previous_pointer(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        mid = "cut-prev"
        _seed_old_active_pointer(iso_discovery)
        _apply_to_smoke_passed(iso_discovery, monkeypatch, mid)

        assert migrate_mod.main(["--cutover", mid]) == 0
        pointer = _read_active_pointer_dict(iso_discovery)
        assert pointer["generation_id"] == mid
        assert pointer["previous_generation_id"] == "v4-old-gen"

        snapshot_path = (
            iso_discovery.settings.DISCOVERY_MIGRATIONS_DIR
            / f"{mid}.previous_pointer.json"
        )
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        assert snapshot["superseded_by"] == mid
        assert snapshot["previous_pointer"]["generation_id"] == "v4-old-gen"

    def test_concurrent_cutover_fails_on_lock(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "cut-concurrent"
        _apply_to_smoke_passed(iso_discovery, monkeypatch, mid)

        lock_path = (
            iso_discovery.settings.DISCOVERY_MIGRATIONS_DIR / ".migration.lock"
        )
        held = FileLock(str(lock_path), timeout=0)
        results: dict[str, int] = {}
        with held:
            worker = threading.Thread(
                target=lambda: results.setdefault(
                    "rc", migrate_mod.main(["--cutover", mid])
                )
            )
            worker.start()
            worker.join(timeout=60)
        assert not worker.is_alive()
        assert results["rc"] == 1
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.SMOKE_PASSED

        # After the lock is released, cutover proceeds normally.
        assert migrate_mod.main(["--cutover", mid]) == 0


class TestPostCutoverChain:
    """A6: cutover → post-cutover-validate → clean-legacy → finalize."""

    def test_full_chain(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        settings = iso_discovery.settings
        mid = "chain-full"
        _apply_to_smoke_passed(
            iso_discovery, monkeypatch, mid,
            archive_impl=_real_archive_impl(settings),
        )
        archive_root = settings.DISCOVERY_LEGACY_ARCHIVE_DIR / mid
        assert archive_root.is_dir()

        assert migrate_mod.main(["--cutover", mid]) == 0

        # Validate is read-only: state must not advance.
        assert migrate_mod.main(["--post-cutover-validate", mid]) == 0
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.CUTOVER_COMMITTED

        assert migrate_mod.main(["--clean-legacy", mid]) == 0
        assert not archive_root.exists()
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.LEGACY_CLEANED
        assert journal.metadata["legacy_cleanup"]["archive_verified"] is True

        assert migrate_mod.main(["--finalize", mid]) == 0
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.FINALIZED
        fin = journal.metadata["finalize"]
        assert fin["active_generation_id"] == mid
        assert len(fin["workspace_manifest_sha256"]) == 64

    def test_validate_rejected_before_cutover(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        mid = "chain-early-validate"
        _apply_to_smoke_passed(iso_discovery, monkeypatch, mid)
        assert migrate_mod.main(["--post-cutover-validate", mid]) == 1

    def test_clean_legacy_rejected_before_cutover(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        mid = "chain-early-clean"
        _apply_to_smoke_passed(iso_discovery, monkeypatch, mid)
        assert migrate_mod.main(["--clean-legacy", mid]) == 1

    def test_finalize_rejected_from_cutover_committed(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        mid = "chain-skip-clean"
        _apply_to_smoke_passed(iso_discovery, monkeypatch, mid)
        assert migrate_mod.main(["--cutover", mid]) == 0
        # clean-legacy was skipped: finalize must be refused.
        assert migrate_mod.main(["--finalize", mid]) == 1

    def test_clean_legacy_rejects_tampered_archive(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        settings = iso_discovery.settings
        mid = "chain-tamper"
        # One legacy pending page gives the archive a verifiable entry.
        page = settings.DISCOVERY_PENDING_PAGES_DIR / "kw" / "p1.json"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            json.dumps({"schema_version": "3.0", "candidates": []}), encoding="utf-8"
        )
        _apply_to_smoke_passed(
            iso_discovery, monkeypatch, mid,
            archive_impl=_real_archive_impl(settings),
            inventory_report=_inventory_report_with_real_pages(iso_discovery, _FAKE_KEYWORDS),
        )
        assert migrate_mod.main(["--cutover", mid]) == 0

        archived = (
            settings.DISCOVERY_LEGACY_ARCHIVE_DIR / mid / "pending_pages"
            / "kw" / "p1.json"
        )
        assert archived.is_file()
        archived.write_text("tampered", encoding="utf-8")

        assert migrate_mod.main(["--clean-legacy", mid]) == 1
        # Archive untouched, state unchanged.
        assert archived.is_file()
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.CUTOVER_COMMITTED


class TestPendingStoreLifecycle:
    """Pending store is a transitional migration channel.

    --post-cutover-validate gates on a fully drained pending store, and
    --clean-legacy removes its directory from the active generation only
    after the drain is proven.
    """

    @staticmethod
    def _seed_active_pending(iso_discovery, mid: str, count: int) -> Path:
        """Write pending candidates into the ACTIVE generation's store."""
        migrate_mod = iso_discovery.migrate_mod
        gen_root = iso_discovery.settings.DISCOVERY_GENERATIONS_DIR / mid
        ws = migrate_mod._workspace_at_root(gen_root)
        nb_path = sorted(ws.keyword_notebook_dir.glob("*.json"))[0]
        keyword_id = json.loads(nb_path.read_text(encoding="utf-8"))["keyword_id"]
        store = PendingCandidateStoreV4(ws)
        for i in range(count):
            store.write(PendingCandidateV4(
                candidate_id=f"cand-{i:03d}",
                keyword_id=keyword_id,
                origin="legacy_candidate_seed",
                doi=f"10.0000/test.{i:03d}",
                normalized_doi=f"10.0000/test.{i:03d}",
                title=f"Candidate {i}",
                created_at="2026-01-01T00:00:00+00:00",
            ))
        return ws.pending_candidates_dir

    def test_validate_fails_when_pending_store_not_drained(
        self, iso_discovery, monkeypatch, capsys
    ):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "pend-validate-block"
        _apply_to_smoke_passed(iso_discovery, monkeypatch, mid)
        assert migrate_mod.main(["--cutover", mid]) == 0
        pending_dir = self._seed_active_pending(iso_discovery, mid, count=2)
        capsys.readouterr()

        rc = migrate_mod.main(["--post-cutover-validate", mid])
        assert rc == 1
        err = capsys.readouterr().err
        assert "not drained" in err
        assert "2 candidate file(s)" in err
        assert "discover_papers_concurrent" in err
        # State and store are untouched; the operator drains and re-runs.
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.CUTOVER_COMMITTED
        assert len(list(pending_dir.rglob("*.json"))) == 2

    def test_validate_passes_after_drain_with_imported_candidates(
        self, iso_discovery, monkeypatch
    ):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "pend-validate-drained"
        _apply_to_smoke_passed(iso_discovery, monkeypatch, mid)
        assert migrate_mod.main(["--cutover", mid]) == 0
        pending_dir = self._seed_active_pending(iso_discovery, mid, count=2)
        # Record that the extraction step imported candidates, then simulate
        # a production discovery drain consuming every candidate file.
        journal = journal_mod.MigrationJournal.load(mid)
        journal.metadata["candidate_stats"] = {"imported": 2}
        journal.save()
        for path in pending_dir.rglob("*.json"):
            path.unlink()

        # The drain legitimately mutated the runtime tree, so the
        # activation-time tree closure no longer applies; identity, manifest,
        # drained-store evidence, and the closed per-seed reconciliation
        # must carry the validation.
        migrations_dir = iso_discovery.settings.DISCOVERY_MIGRATIONS_DIR
        (migrations_dir / f"{mid}.post_cutover_reconciliation.json").write_text(
            json.dumps({
                "schema_version": "1.0",
                "migration_id": mid,
                "unresolved_items": 0,
                "receipts_verified": 2,
            }),
            encoding="utf-8",
        )
        receipts_dir = migrations_dir / f"{mid}.receipts"
        receipts_dir.mkdir()
        for i in range(2):
            (receipts_dir / f"{i:032x}.json").write_text("{}", encoding="utf-8")
        assert migrate_mod.main(["--post-cutover-validate", mid]) == 0
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.CUTOVER_COMMITTED

    def test_validate_requires_receipts_for_imported_candidates(
        self, iso_discovery, monkeypatch
    ):
        """An empty pending store alone is never sufficient proof."""
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "pend-validate-no-receipts"
        _apply_to_smoke_passed(iso_discovery, monkeypatch, mid)
        assert migrate_mod.main(["--cutover", mid]) == 0
        pending_dir = self._seed_active_pending(iso_discovery, mid, count=2)
        journal = journal_mod.MigrationJournal.load(mid)
        journal.metadata["candidate_stats"] = {"imported": 2}
        journal.save()
        for path in pending_dir.rglob("*.json"):
            path.unlink()

        # Drained store but no reconciliation artifacts: validation fails.
        assert migrate_mod.main(["--post-cutover-validate", mid]) == 1

        # A reconciliation that does not cover the imported set also fails.
        migrations_dir = iso_discovery.settings.DISCOVERY_MIGRATIONS_DIR
        (migrations_dir / f"{mid}.post_cutover_reconciliation.json").write_text(
            json.dumps({
                "schema_version": "1.0",
                "migration_id": mid,
                "unresolved_items": 0,
                "receipts_verified": 1,
            }),
            encoding="utf-8",
        )
        assert migrate_mod.main(["--post-cutover-validate", mid]) == 1

    def test_clean_legacy_refuses_undrained_pending_store(
        self, iso_discovery, monkeypatch, capsys
    ):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        settings = iso_discovery.settings
        mid = "pend-clean-block"
        _apply_to_smoke_passed(
            iso_discovery, monkeypatch, mid,
            archive_impl=_real_archive_impl(settings),
        )
        assert migrate_mod.main(["--cutover", mid]) == 0
        pending_dir = self._seed_active_pending(iso_discovery, mid, count=1)
        archive_root = settings.DISCOVERY_LEGACY_ARCHIVE_DIR / mid
        capsys.readouterr()

        rc = migrate_mod.main(["--clean-legacy", mid])
        assert rc == 1
        err = capsys.readouterr().err
        assert "not drained" in err
        assert "refusing to remove" in err
        # Nothing was deleted and the state did not advance.
        assert pending_dir.is_dir()
        assert len(list(pending_dir.rglob("*.json"))) == 1
        assert archive_root.is_dir()
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.CUTOVER_COMMITTED

    def test_clean_legacy_removes_drained_pending_store(
        self, iso_discovery, monkeypatch
    ):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        settings = iso_discovery.settings
        mid = "pend-clean-ok"
        _apply_to_smoke_passed(
            iso_discovery, monkeypatch, mid,
            archive_impl=_real_archive_impl(settings),
        )
        assert migrate_mod.main(["--cutover", mid]) == 0
        gen_root = settings.DISCOVERY_GENERATIONS_DIR / mid
        pending_dir = gen_root / "pending_candidates"
        assert pending_dir.is_dir()

        assert migrate_mod.main(["--clean-legacy", mid]) == 0
        assert not pending_dir.exists()
        journal = journal_mod.MigrationJournal.load(mid)
        cleanup = journal.metadata["legacy_cleanup"]
        assert cleanup["pending_store_drained"] is True
        assert cleanup["pending_candidates_dir_removed"] == str(pending_dir)

        # The active generation resolves without the transitional directory:
        # finalize and the production resolve path are unaffected.
        assert migrate_mod.main(["--finalize", mid]) == 0
        ws = migrate_mod.WorkspaceResolver().resolve_active()
        assert ws.generation_id == mid


class TestRollback:
    """A6: --rollback from cutover_committed only."""

    def test_rollback_restores_previous_pointer(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        settings = iso_discovery.settings
        mid = "rb-prev"
        _seed_old_active_pointer(iso_discovery)
        _apply_to_smoke_passed(iso_discovery, monkeypatch, mid)
        assert migrate_mod.main(["--cutover", mid]) == 0

        assert migrate_mod.main(["--rollback", mid]) == 0
        pointer = _read_active_pointer_dict(iso_discovery)
        assert pointer["generation_id"] == "v4-old-gen"
        # The promoted generation is back in staging.
        assert not (settings.DISCOVERY_GENERATIONS_DIR / mid).exists()
        assert (settings.DISCOVERY_STAGING_DIR / mid).is_dir()
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.ABORTED
        assert journal.metadata["rollback"]["restored_previous_pointer"] is True

    def test_rollback_without_previous_removes_pointer(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        settings = iso_discovery.settings
        mid = "rb-first"
        _apply_to_smoke_passed(iso_discovery, monkeypatch, mid)
        assert migrate_mod.main(["--cutover", mid]) == 0

        assert migrate_mod.main(["--rollback", mid]) == 0
        assert not settings.DISCOVERY_ACTIVE_GENERATION_PATH.exists()
        assert (settings.DISCOVERY_STAGING_DIR / mid).is_dir()
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.ABORTED
        assert journal.metadata["rollback"]["restored_previous_pointer"] is False

    def test_rollback_rejected_pre_cutover(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        mid = "rb-early"
        _apply_to_smoke_passed(iso_discovery, monkeypatch, mid)
        assert migrate_mod.main(["--rollback", mid]) == 1

    def test_rollback_rejected_after_legacy_cleaned(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        settings = iso_discovery.settings
        mid = "rb-late"
        _apply_to_smoke_passed(
            iso_discovery, monkeypatch, mid,
            archive_impl=_real_archive_impl(settings),
        )
        assert migrate_mod.main(["--cutover", mid]) == 0
        assert migrate_mod.main(["--clean-legacy", mid]) == 0
        assert migrate_mod.main(["--rollback", mid]) == 1

    def test_abort_rejected_after_rollback(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        mid = "rb-abort"
        _apply_to_smoke_passed(iso_discovery, monkeypatch, mid)
        assert migrate_mod.main(["--cutover", mid]) == 0
        assert migrate_mod.main(["--rollback", mid]) == 0
        assert migrate_mod.main(["--abort", mid]) == 1


class TestResumePostCutover:
    """A6: resume must not treat post-cutover states as terminal."""

    def test_resume_at_cutover_committed_guides_next_steps(
        self, iso_discovery, monkeypatch, capsys
    ):
        migrate_mod = iso_discovery.migrate_mod
        mid = "resume-cut"
        _apply_to_smoke_passed(iso_discovery, monkeypatch, mid)
        assert migrate_mod.main(["--cutover", mid]) == 0
        capsys.readouterr()

        rc = migrate_mod.main(["--resume", mid])
        assert rc == 0
        out = capsys.readouterr().out
        assert "--post-cutover-validate" in out
        assert "--clean-legacy" in out
        assert "terminal state" not in out

    def test_resume_at_legacy_cleaned_guides_finalize(
        self, iso_discovery, monkeypatch, capsys
    ):
        migrate_mod = iso_discovery.migrate_mod
        settings = iso_discovery.settings
        mid = "resume-cleaned"
        _apply_to_smoke_passed(
            iso_discovery, monkeypatch, mid,
            archive_impl=_real_archive_impl(settings),
        )
        assert migrate_mod.main(["--cutover", mid]) == 0
        assert migrate_mod.main(["--clean-legacy", mid]) == 0
        capsys.readouterr()

        rc = migrate_mod.main(["--resume", mid])
        assert rc == 0
        out = capsys.readouterr().out
        assert "--finalize" in out
        assert "terminal state" not in out

    def test_resume_at_finalized_is_terminal(self, iso_discovery, monkeypatch, capsys):
        migrate_mod = iso_discovery.migrate_mod
        settings = iso_discovery.settings
        mid = "resume-final"
        _apply_to_smoke_passed(
            iso_discovery, monkeypatch, mid,
            archive_impl=_real_archive_impl(settings),
        )
        assert migrate_mod.main(["--cutover", mid]) == 0
        assert migrate_mod.main(["--clean-legacy", mid]) == 0
        assert migrate_mod.main(["--finalize", mid]) == 0
        capsys.readouterr()

        rc = migrate_mod.main(["--resume", mid])
        assert rc == 0
        out = capsys.readouterr().out
        assert "terminal state" in out


class TestWorkspaceManifestTruth:
    """A7: manifest fields are computed live from real workspace content."""

    def test_manifest_fields_are_real_computed_values(self, iso_discovery):
        migrate_mod = iso_discovery.migrate_mod
        staging_ws = migrate_mod.create_staging_workspace("manifest-truth")
        _write_notebook(staging_ws.keyword_notebook_dir, _ready_notebook("测试甲"))
        (staging_ws.lane_states_dir / "lane1.json").write_text(
            "{}", encoding="utf-8"
        )
        pj_dir = staging_ws.page_journals_dir / "kw"
        pj_dir.mkdir(parents=True)
        (pj_dir / "p1.json").write_text("{}", encoding="utf-8")
        (pj_dir / "p2.json").write_text("{}", encoding="utf-8")

        manifest, manifest_sha = migrate_mod._build_workspace_manifest(
            staging_ws, "manifest-truth", None, 8,
        )

        empty_set = migrate_mod._sha256_text("")
        assert manifest.page_journal_count == 2
        assert manifest.lane_state_set_hash != empty_set
        assert manifest.lane_state_set_hash == migrate_mod._hash_directory(
            staging_ws.lane_states_dir
        )
        assert manifest.page_journal_set_hash != empty_set
        assert manifest.page_journal_set_hash == migrate_mod._hash_directory(
            staging_ws.page_journals_dir
        )
        assert manifest.workspace_tree_sha256 == migrate_mod._hash_directory(
            staging_ws.root, exclude={"workspace.json"}
        )
        assert len(manifest_sha) == 64

    def test_manifest_empty_dirs_yield_computed_empty_tree_hash(self, iso_discovery):
        migrate_mod = iso_discovery.migrate_mod
        staging_ws = migrate_mod.create_staging_workspace("manifest-empty")
        manifest, _ = migrate_mod._build_workspace_manifest(
            staging_ws, "manifest-empty", None, 0,
        )
        # Empty dirs produce the deterministic empty-tree hash (computed,
        # never a hard-coded constant) and zero counts.
        assert manifest.lane_state_set_hash == migrate_mod._hash_directory(
            staging_ws.lane_states_dir
        )
        assert manifest.page_journal_set_hash == migrate_mod._hash_directory(
            staging_ws.page_journals_dir
        )
        assert manifest.page_journal_count == 0


class TestPostCutoverTreeVerification:
    """A7: --post-cutover-validate verifies the activation-time tree closure."""

    def test_validate_detects_tree_tamper(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        mid = "tree-tamper"
        _apply_to_smoke_passed(iso_discovery, monkeypatch, mid)
        assert migrate_mod.main(["--cutover", mid]) == 0

        gen_root = iso_discovery.settings.DISCOVERY_GENERATIONS_DIR / mid
        nb_path = next((gen_root / "keyword_notebooks").glob("*.json"))
        nb_path.write_text("tampered", encoding="utf-8")

        assert migrate_mod.main(["--post-cutover-validate", mid]) == 1
        journal = iso_discovery.journal_mod.MigrationJournal.load(mid)
        assert journal.state == iso_discovery.journal_mod.MigrationState.CUTOVER_COMMITTED

    def test_production_resolve_unaffected_by_runtime_writes(
        self, iso_discovery, monkeypatch
    ):
        migrate_mod = iso_discovery.migrate_mod
        mid = "tree-runtime"
        _apply_to_smoke_passed(iso_discovery, monkeypatch, mid)
        assert migrate_mod.main(["--cutover", mid]) == 0
        # Immediately after cutover the tree closure still validates.
        assert migrate_mod.main(["--post-cutover-validate", mid]) == 0

        # Simulate a normal production discovery run mutating the workspace.
        gen_root = iso_discovery.settings.DISCOVERY_GENERATIONS_DIR / mid
        (gen_root / "reports" / "run.json").write_text("{}", encoding="utf-8")
        (gen_root / "lane_states" / "lane.json").write_text("{}", encoding="utf-8")

        # The production resolve path (every discovery CLI startup) must not
        # be rejected by runtime content drift.
        ws = migrate_mod.WorkspaceResolver().resolve_active()
        assert ws.generation_id == mid


class TestArchiveConsistency:
    """B2: archive copies are verified byte-for-byte after copying."""

    @staticmethod
    def _seed_pages(settings) -> None:
        page = settings.DISCOVERY_PENDING_PAGES_DIR / "kw" / "p1.json"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            json.dumps({"schema_version": "3.0", "candidates": []}),
            encoding="utf-8",
        )

    def test_archive_roundtrip_records_verified_real_hashes(self, iso_discovery):
        from src.migrations.discovery_v4 import archive_builder as ab

        settings = iso_discovery.settings
        self._seed_pages(settings)
        (settings.DISCOVERY_KEYWORD_NOTEBOOK_DIR / "nb.json").write_text(
            "{}", encoding="utf-8"
        )

        result = ab.prepare_legacy_archive("arc-ok")
        assert result["pending_pages_total"] == 1
        assert result["notebooks_total"] == 1
        pages = result["pending_pages"]
        assert pages["total_size_bytes"] > 0
        assert len(pages["aggregate_sha256"]) == 64
        notebooks = result["keyword_notebooks"]
        assert notebooks["total_size_bytes"] > 0
        assert len(notebooks["aggregate_sha256"]) == 64

    def test_dest_tampered_during_copy_fails(self, iso_discovery, monkeypatch):
        from src.migrations.discovery_v4 import archive_builder as ab

        settings = iso_discovery.settings
        self._seed_pages(settings)

        real_copy2 = ab.shutil.copy2

        def corrupting_copy2(src, dst):
            real_copy2(src, dst)
            Path(dst).write_text("corrupted", encoding="utf-8")

        monkeypatch.setattr(ab.shutil, "copy2", corrupting_copy2)
        with pytest.raises(ab.ArchiveVerificationError, match="hash mismatch"):
            ab.prepare_legacy_archive("arc-tamper")
        # No fake archived state: the partial archive root is removed so a
        # resume can retry the archive step.
        assert not (settings.DISCOVERY_LEGACY_ARCHIVE_DIR / "arc-tamper").exists()

    def test_missing_dest_after_copy_fails(self, iso_discovery, monkeypatch):
        from src.migrations.discovery_v4 import archive_builder as ab

        settings = iso_discovery.settings
        self._seed_pages(settings)

        monkeypatch.setattr(ab.shutil, "copy2", lambda src, dst: None)
        with pytest.raises(ab.ArchiveVerificationError, match="missing after copy"):
            ab.prepare_legacy_archive("arc-missing")
        assert not (settings.DISCOVERY_LEGACY_ARCHIVE_DIR / "arc-missing").exists()

    def test_truncated_copy_fails(self, iso_discovery, monkeypatch):
        from src.migrations.discovery_v4 import archive_builder as ab

        settings = iso_discovery.settings
        self._seed_pages(settings)

        def truncating_copy2(src, dst):
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            Path(dst).write_text("", encoding="utf-8")

        monkeypatch.setattr(ab.shutil, "copy2", truncating_copy2)
        with pytest.raises(ab.ArchiveVerificationError):
            ab.prepare_legacy_archive("arc-trunc")
        assert not (settings.DISCOVERY_LEGACY_ARCHIVE_DIR / "arc-trunc").exists()

    def test_stray_file_in_archive_fails_count_check(self, iso_discovery):
        from src.migrations.discovery_v4 import archive_builder as ab

        settings = iso_discovery.settings
        self._seed_pages(settings)
        archive_root = settings.DISCOVERY_LEGACY_ARCHIVE_DIR / "arc-stray"
        target = archive_root / "pending_pages"
        target.mkdir(parents=True)
        (target / "stray.json").write_text("{}", encoding="utf-8")

        with pytest.raises(ab.ArchiveVerificationError, match="count mismatch"):
            ab.archive_pending_pages(
                settings.DISCOVERY_PENDING_PAGES_DIR, archive_root, "arc-stray"
            )


class TestNotebookMigrationFromArchiveSnapshot:
    """B2: notebook migration reads the archive snapshot, not the live dir."""

    def test_step_reads_archive_snapshot_dir(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        captured: dict[str, str] = {}

        def capturing_migrate(notebook_dir, output_dir):
            captured["notebook_dir"] = str(notebook_dir)
            return _fake_migrate_all_notebooks(notebook_dir, output_dir)

        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report",
            lambda **_: _minimal_inventory_report(_FAKE_KEYWORDS),
        )
        monkeypatch.setattr(
            migrate_mod, "prepare_legacy_archive",
            lambda mid: _minimal_archive_result(iso_discovery, mid),
        )
        monkeypatch.setattr(migrate_mod, "migrate_all_notebooks", capturing_migrate)
        monkeypatch.setattr(discover_mod, "main_internal", lambda argv: 0)

        rc = migrate_mod.main([
            "--apply", "--migration-id", "nb-snapshot-test",
        ])
        assert rc == 0
        expected = (
            iso_discovery.settings.DISCOVERY_LEGACY_ARCHIVE_DIR
            / "nb-snapshot-test" / "keyword_notebooks"
        )
        assert captured["notebook_dir"] == str(expected)

    def test_missing_snapshot_fails_closed(self, iso_discovery):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "nb-no-snapshot"
        staging_ws = migrate_mod.create_staging_workspace(mid)
        journal = journal_mod.MigrationJournal.create(mid)
        for s in [
            journal_mod.MigrationState.INVENTORY_COMPLETE,
            journal_mod.MigrationState.ARCHIVE_PREPARED,
            journal_mod.MigrationState.WORKSPACE_BUILT,
        ]:
            journal.transition_to(s)
        journal.metadata["staging_workspace"] = str(staging_ws.root)
        journal.save()

        args = migrate_mod._parse_args(["--apply"])
        with pytest.raises(migrate_mod.MigrationStepError, match="snapshot missing"):
            migrate_mod._step_migrate_notebooks(journal, staging_ws, args)
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.WORKSPACE_BUILT

    def test_real_migration_uses_snapshot_after_source_removed(
        self, iso_discovery, monkeypatch
    ):
        """Real archive + real notebook migration: wiping the live notebook
        directory after archiving must not change the migration input."""
        migrate_mod = iso_discovery.migrate_mod
        settings = iso_discovery.settings
        for kw in _FAKE_KEYWORDS:
            _write_notebook(
                settings.DISCOVERY_KEYWORD_NOTEBOOK_DIR, _ready_v3_notebook(kw)
            )

        from src.migrations.discovery_v4 import archive_builder as ab
        real_prepare = ab.prepare_legacy_archive

        def prepare_then_wipe(mid: str):
            result = real_prepare(mid)
            for p in settings.DISCOVERY_KEYWORD_NOTEBOOK_DIR.glob("*.json"):
                p.unlink()
            return result

        real_migrate = migrate_mod.migrate_all_notebooks
        captured: dict[str, str] = {}

        def capturing_migrate(notebook_dir, output_dir):
            captured["notebook_dir"] = str(notebook_dir)
            return real_migrate(notebook_dir=notebook_dir, output_dir=output_dir)

        monkeypatch.setattr(migrate_mod, "prepare_legacy_archive", prepare_then_wipe)
        monkeypatch.setattr(migrate_mod, "migrate_all_notebooks", capturing_migrate)
        monkeypatch.setattr(discover_mod, "main_internal", lambda argv: 0)

        rc = migrate_mod.main([
            "--apply", "--migration-id", "nb-real-snapshot",
        ])
        assert rc == 0
        expected = (
            settings.DISCOVERY_LEGACY_ARCHIVE_DIR
            / "nb-real-snapshot" / "keyword_notebooks"
        )
        assert captured["notebook_dir"] == str(expected)
        journal = iso_discovery.journal_mod.MigrationJournal.load("nb-real-snapshot")
        assert journal.state == iso_discovery.journal_mod.MigrationState.SMOKE_PASSED
        assert journal.metadata["notebooks_migrated"] == len(_FAKE_KEYWORDS)

    def test_resume_replays_notebook_step_idempotently(
        self, iso_discovery, monkeypatch
    ):
        """Crash after notebook files written but before journal save: resume
        re-runs the step and converges to the same state."""
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "nb-resume"
        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report",
            lambda **_: _minimal_inventory_report(_FAKE_KEYWORDS),
        )
        monkeypatch.setattr(
            migrate_mod, "prepare_legacy_archive",
            lambda m: _minimal_archive_result(iso_discovery, m),
        )
        monkeypatch.setattr(
            migrate_mod, "migrate_all_notebooks", _fake_migrate_all_notebooks
        )
        monkeypatch.setattr(discover_mod, "main_internal", lambda argv: 0)

        original_save = journal_mod.MigrationJournal.save
        crashed = {"done": False}

        def flaky_save(self):
            if (
                self.state == journal_mod.MigrationState.NOTEBOOKS_STAGED
                and not crashed["done"]
            ):
                crashed["done"] = True
                raise RuntimeError("simulated crash before journal save")
            return original_save(self)

        monkeypatch.setattr(journal_mod.MigrationJournal, "save", flaky_save)
        with pytest.raises(RuntimeError, match="simulated crash"):
            migrate_mod.main([
                "--apply", "--migration-id", mid,
            ])

        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.WORKSPACE_BUILT

        rc = migrate_mod.main(["--resume", mid])
        assert rc == 0
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.SMOKE_PASSED
        assert journal.metadata["notebooks_migrated"] == len(_FAKE_KEYWORDS)
        staged = sorted(
            p.name
            for p in (
                Path(journal.metadata["staging_workspace"]) / "keyword_notebooks"
            ).glob("*.json")
        )
        assert len(staged) == len(_FAKE_KEYWORDS)


# ── Phase 4: maintenance lock, crash-resume idempotency, binding ─────────


def _dead_pid() -> int:
    """Return a PID that is guaranteed dead (a process that already exited)."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    proc.wait(timeout=30)
    from src.mineru_lock import _is_pid_alive

    assert proc.pid is not None
    assert not _is_pid_alive(proc.pid)
    return proc.pid


def _lock_sidecar_path(migrations_dir: Path) -> Path:
    return migrations_dir / ".migration.lock.info.json"


def _write_lock_sidecar(migrations_dir: Path, pid: int, *, purpose: str = "apply") -> Path:
    sidecar = _lock_sidecar_path(migrations_dir)
    sidecar.write_text(
        json.dumps({
            "pid": pid,
            "purpose": purpose,
            "migration_id": "v4-stale-test",
            "acquired_at": "2026-01-01T00:00:00+00:00",
            "command": "simulated",
        }),
        encoding="utf-8",
    )
    return sidecar


def _run_in_thread(fn) -> dict:
    results: dict[str, int] = {}
    worker = threading.Thread(target=lambda: results.setdefault("rc", fn()))
    worker.start()
    worker.join(timeout=60)
    assert not worker.is_alive()
    return results


class TestMaintenanceLockMutualExclusion:
    """apply/resume/cutover/rollback fail closed while the lock is held."""

    def test_apply_fails_closed_while_lock_held(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        lock_path = iso_discovery.settings.DISCOVERY_MIGRATIONS_DIR / ".migration.lock"
        held = FileLock(str(lock_path), timeout=0)
        with held:
            results = _run_in_thread(
                lambda: migrate_mod.main(["--apply", "--migration-id", "lock-apply"])
            )
        assert results["rc"] == 1
        # No journal was created while the lock was held.
        assert not (
            iso_discovery.settings.DISCOVERY_MIGRATIONS_DIR / "lock-apply.json"
        ).exists()

    def test_cutover_fails_closed_while_lock_held(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "lock-cutover"
        _apply_to_smoke_passed(iso_discovery, monkeypatch, mid)

        lock_path = iso_discovery.settings.DISCOVERY_MIGRATIONS_DIR / ".migration.lock"
        held = FileLock(str(lock_path), timeout=0)
        with held:
            results = _run_in_thread(lambda: migrate_mod.main(["--cutover", mid]))
        assert results["rc"] == 1
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.SMOKE_PASSED

        # After the lock is released, cutover proceeds normally.
        assert migrate_mod.main(["--cutover", mid]) == 0

    def test_rollback_fails_closed_while_lock_held(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "lock-rollback"
        _apply_to_smoke_passed(iso_discovery, monkeypatch, mid)
        assert migrate_mod.main(["--cutover", mid]) == 0

        lock_path = iso_discovery.settings.DISCOVERY_MIGRATIONS_DIR / ".migration.lock"
        held = FileLock(str(lock_path), timeout=0)
        with held:
            results = _run_in_thread(lambda: migrate_mod.main(["--rollback", mid]))
        assert results["rc"] == 1
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.CUTOVER_COMMITTED

        assert migrate_mod.main(["--rollback", mid]) == 0

    def test_lock_released_after_apply(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        mid = "lock-release"
        _apply_to_smoke_passed(iso_discovery, monkeypatch, mid)
        sidecar = _lock_sidecar_path(iso_discovery.settings.DISCOVERY_MIGRATIONS_DIR)
        assert not sidecar.exists()


class TestMaintenanceLockFullCommandCoverage:
    """abort/clean-legacy/finalize also fail closed while the lock is held."""

    def test_abort_fails_closed_while_lock_held(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "lock-abort"
        _apply_to_smoke_passed(iso_discovery, monkeypatch, mid)

        lock_path = iso_discovery.settings.DISCOVERY_MIGRATIONS_DIR / ".migration.lock"
        held = FileLock(str(lock_path), timeout=0)
        with held:
            results = _run_in_thread(lambda: migrate_mod.main(["--abort", mid]))
        assert results["rc"] == 1
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.SMOKE_PASSED

        # After the lock is released, abort proceeds normally.
        assert migrate_mod.main(["--abort", mid]) == 0
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.ABORTED

    def test_clean_legacy_fails_closed_while_lock_held(
        self, iso_discovery, monkeypatch
    ):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        settings = iso_discovery.settings
        mid = "lock-clean"
        _apply_to_smoke_passed(
            iso_discovery, monkeypatch, mid,
            archive_impl=_real_archive_impl(settings),
        )
        archive_root = settings.DISCOVERY_LEGACY_ARCHIVE_DIR / mid
        assert archive_root.is_dir()
        assert migrate_mod.main(["--cutover", mid]) == 0

        lock_path = settings.DISCOVERY_MIGRATIONS_DIR / ".migration.lock"
        held = FileLock(str(lock_path), timeout=0)
        with held:
            results = _run_in_thread(
                lambda: migrate_mod.main(["--clean-legacy", mid])
            )
        assert results["rc"] == 1
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.CUTOVER_COMMITTED
        # Nothing was deleted while the lock was held.
        assert archive_root.is_dir()

        assert migrate_mod.main(["--clean-legacy", mid]) == 0
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.LEGACY_CLEANED

    def test_finalize_fails_closed_while_lock_held(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        settings = iso_discovery.settings
        mid = "lock-finalize"
        _apply_to_smoke_passed(
            iso_discovery, monkeypatch, mid,
            archive_impl=_real_archive_impl(settings),
        )
        assert migrate_mod.main(["--cutover", mid]) == 0
        assert migrate_mod.main(["--clean-legacy", mid]) == 0

        lock_path = settings.DISCOVERY_MIGRATIONS_DIR / ".migration.lock"
        held = FileLock(str(lock_path), timeout=0)
        with held:
            results = _run_in_thread(lambda: migrate_mod.main(["--finalize", mid]))
        assert results["rc"] == 1
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.LEGACY_CLEANED

        assert migrate_mod.main(["--finalize", mid]) == 0
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.FINALIZED


class TestMaintenanceLockStaleTakeover:
    """A sidecar left by a dead process never blocks; a live owner does."""

    def test_apply_takes_over_stale_lock(self, iso_discovery, monkeypatch):
        migrations_dir = iso_discovery.settings.DISCOVERY_MIGRATIONS_DIR
        sidecar = _write_lock_sidecar(migrations_dir, _dead_pid())
        _apply_to_smoke_passed(iso_discovery, monkeypatch, "stale-apply")
        # The stale sidecar was taken over and released by the apply run.
        assert not sidecar.exists()

    def test_resume_takes_over_stale_lock(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "stale-resume"
        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report",
            lambda **_: _minimal_inventory_report(_FAKE_KEYWORDS),
        )
        monkeypatch.setattr(
            migrate_mod, "prepare_legacy_archive",
            lambda m: _minimal_archive_result(iso_discovery, m),
        )
        monkeypatch.setattr(migrate_mod, "migrate_all_notebooks", _fake_migrate_all_notebooks)
        monkeypatch.setattr(discover_mod, "main_internal", lambda argv: 1)
        assert migrate_mod.main(["--apply", "--migration-id", mid]) == 1
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.SMOKE_FAILED

        # Simulate the crashed apply's leftover lock, then resume.
        migrations_dir = iso_discovery.settings.DISCOVERY_MIGRATIONS_DIR
        sidecar = _write_lock_sidecar(migrations_dir, _dead_pid())
        monkeypatch.setattr(discover_mod, "main_internal", lambda argv: 0)
        assert migrate_mod.main(["--resume", mid]) == 0
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.SMOKE_PASSED
        assert not sidecar.exists()

    def test_live_owner_blocks_apply_cutover_rollback(
        self, iso_discovery, monkeypatch
    ):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "live-owner"
        _apply_to_smoke_passed(iso_discovery, monkeypatch, mid)

        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        try:
            migrations_dir = iso_discovery.settings.DISCOVERY_MIGRATIONS_DIR
            sidecar = _write_lock_sidecar(migrations_dir, proc.pid)

            # A second apply fails closed while the live owner holds the lock.
            assert migrate_mod.main(["--apply", "--migration-id", "live-owner-2"]) == 1
            assert not (migrations_dir / "live-owner-2.json").exists()
            # Cutover fails closed.
            assert migrate_mod.main(["--cutover", mid]) == 1
            journal = journal_mod.MigrationJournal.load(mid)
            assert journal.state == journal_mod.MigrationState.SMOKE_PASSED

            # Once the owner is gone, cutover and rollback work again.
            sidecar.unlink()
            assert migrate_mod.main(["--cutover", mid]) == 0
            _write_lock_sidecar(migrations_dir, proc.pid)
            assert migrate_mod.main(["--rollback", mid]) == 1
            journal = journal_mod.MigrationJournal.load(mid)
            assert journal.state == journal_mod.MigrationState.CUTOVER_COMMITTED
            _lock_sidecar_path(migrations_dir).unlink()
            assert migrate_mod.main(["--rollback", mid]) == 0
        finally:
            assert proc.stdin is not None
            proc.stdin.close()
            proc.wait(timeout=30)


class TestApplyCrashResumeIdempotency:
    """Kill-and-resume at every apply crash point converges idempotently."""

    @staticmethod
    def _mock_steps(iso_discovery, monkeypatch, *, smoke_rc: int = 0):
        migrate_mod = iso_discovery.migrate_mod
        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report",
            lambda **_: _minimal_inventory_report(_FAKE_KEYWORDS),
        )
        monkeypatch.setattr(
            migrate_mod, "prepare_legacy_archive",
            lambda m: _minimal_archive_result(iso_discovery, m),
        )
        monkeypatch.setattr(migrate_mod, "migrate_all_notebooks", _fake_migrate_all_notebooks)
        monkeypatch.setattr(discover_mod, "main_internal", lambda argv: smoke_rc)

    def test_crash_during_inventory_resume_completes(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "crash-inventory"
        self._mock_steps(iso_discovery, monkeypatch)

        calls = {"n": 0}
        real_inventory = migrate_mod.generate_inventory_report

        def flaky_inventory(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated crash during inventory")
            return real_inventory(**kwargs)

        monkeypatch.setattr(migrate_mod, "generate_inventory_report", flaky_inventory)
        with pytest.raises(RuntimeError, match="simulated crash"):
            migrate_mod.main(["--apply", "--migration-id", mid])
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.PLANNED

        assert migrate_mod.main(["--resume", mid]) == 0
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.SMOKE_PASSED

    @staticmethod
    def _seed_real_page(iso_discovery) -> Path:
        page = (
            iso_discovery.settings.DISCOVERY_PENDING_PAGES_DIR / "kw" / "p1.json"
        )
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            json.dumps({"schema_version": "3.0", "candidates": []}), encoding="utf-8"
        )
        return page

    def _mock_real_archive_steps(self, iso_discovery, monkeypatch):
        """Mocks for a real-archive apply: hybrid inventory + fake notebooks."""
        migrate_mod = iso_discovery.migrate_mod
        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report",
            lambda **_: _inventory_report_with_real_pages(iso_discovery, _FAKE_KEYWORDS),
        )
        monkeypatch.setattr(migrate_mod, "migrate_all_notebooks", _fake_migrate_all_notebooks)
        monkeypatch.setattr(discover_mod, "main_internal", lambda argv: 0)

    def test_crash_after_archive_reuses_verified_leftover(
        self, iso_discovery, monkeypatch, capsys
    ):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "crash-archive-saved"
        self._seed_real_page(iso_discovery)
        self._mock_real_archive_steps(iso_discovery, monkeypatch)

        original_save = journal_mod.MigrationJournal.save
        crashed = {"done": False}

        def flaky_save(self):
            if (
                self.state == journal_mod.MigrationState.ARCHIVE_PREPARED
                and not crashed["done"]
            ):
                crashed["done"] = True
                raise RuntimeError("simulated crash after archive before journal save")
            return original_save(self)

        monkeypatch.setattr(journal_mod.MigrationJournal, "save", flaky_save)
        with pytest.raises(RuntimeError, match="simulated crash"):
            migrate_mod.main(["--apply", "--migration-id", mid])
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.INVENTORY_COMPLETE
        archive_root = iso_discovery.settings.DISCOVERY_LEGACY_ARCHIVE_DIR / mid
        assert (archive_root / "pending_pages" / "archive_manifest.json").is_file()

        capsys.readouterr()
        assert migrate_mod.main(["--resume", mid]) == 0
        out = capsys.readouterr().out
        assert "Reusing verified archive from previous attempt" in out
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.SMOKE_PASSED

    def test_crash_mid_archive_rebuilds_partial_leftover(
        self, iso_discovery, monkeypatch, capsys
    ):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "crash-archive-partial"
        self._seed_real_page(iso_discovery)
        self._mock_real_archive_steps(iso_discovery, monkeypatch)
        real_prepare = migrate_mod.prepare_legacy_archive

        def partial_archive(migration_id: str):
            root = iso_discovery.settings.DISCOVERY_LEGACY_ARCHIVE_DIR / migration_id
            root.mkdir(parents=True, exist_ok=True)
            (root / "junk.json").write_text("partial", encoding="utf-8")
            raise RuntimeError("simulated crash mid archive copy")

        monkeypatch.setattr(migrate_mod, "prepare_legacy_archive", partial_archive)
        with pytest.raises(RuntimeError, match="simulated crash"):
            migrate_mod.main(["--apply", "--migration-id", mid])
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.INVENTORY_COMPLETE

        monkeypatch.setattr(migrate_mod, "prepare_legacy_archive", real_prepare)
        capsys.readouterr()
        assert migrate_mod.main(["--resume", mid]) == 0
        out = capsys.readouterr().out
        assert "Discarding invalid leftover archive" in out
        archive_root = iso_discovery.settings.DISCOVERY_LEGACY_ARCHIVE_DIR / mid
        assert not (archive_root / "junk.json").exists()
        assert (archive_root / "pending_pages" / "archive_manifest.json").is_file()
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.SMOKE_PASSED

    def test_crash_after_staging_mkdir_reuses_workspace(
        self, iso_discovery, monkeypatch, capsys
    ):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "crash-staging"
        self._mock_steps(iso_discovery, monkeypatch)

        original_save = journal_mod.MigrationJournal.save
        crashed = {"done": False}

        def flaky_save(self):
            if (
                self.state == journal_mod.MigrationState.WORKSPACE_BUILT
                and not crashed["done"]
            ):
                crashed["done"] = True
                raise RuntimeError("simulated crash after staging mkdir")
            return original_save(self)

        monkeypatch.setattr(journal_mod.MigrationJournal, "save", flaky_save)
        with pytest.raises(RuntimeError, match="simulated crash"):
            migrate_mod.main(["--apply", "--migration-id", mid])
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.ARCHIVE_PREPARED
        staging_root = iso_discovery.settings.DISCOVERY_STAGING_DIR / mid
        assert staging_root.is_dir()

        capsys.readouterr()
        assert migrate_mod.main(["--resume", mid]) == 0
        out = capsys.readouterr().out
        assert "Reusing existing staging workspace" in out
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.SMOKE_PASSED

    def test_crash_mid_notebook_write_resume_replays(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "crash-notebook-write"
        self._mock_steps(iso_discovery, monkeypatch)

        crashed = {"done": False}

        def failing_migrate(notebook_dir, output_dir):
            if not crashed["done"]:
                crashed["done"] = True
                # One notebook lands on disk before the crash.
                results = _fake_migrate_all_notebooks(notebook_dir, output_dir)
                first = sorted(Path(output_dir).glob("*.json"))[:1]
                for extra in sorted(Path(output_dir).glob("*.json"))[1:]:
                    extra.unlink()
                assert first
                raise RuntimeError("simulated crash mid notebook write")
            return _fake_migrate_all_notebooks(notebook_dir, output_dir)

        monkeypatch.setattr(migrate_mod, "migrate_all_notebooks", failing_migrate)
        with pytest.raises(RuntimeError, match="simulated crash"):
            migrate_mod.main(["--apply", "--migration-id", mid])
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.WORKSPACE_BUILT

        assert migrate_mod.main(["--resume", mid]) == 0
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.SMOKE_PASSED
        staged = sorted(
            (Path(journal.metadata["staging_workspace"]) / "keyword_notebooks").glob("*.json")
        )
        assert len(staged) == len(_FAKE_KEYWORDS)

    def test_crash_mid_candidate_extraction_resume_consistent(
        self, iso_discovery, monkeypatch
    ):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "crash-extract"
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
        journal.metadata["notebooks_failed"] = 0
        nb = _ready_notebook("测试甲")
        kid = nb["keyword_id"]
        _write_notebook(staging_ws.keyword_notebook_dir, nb)
        journal.metadata["inventory_enabled_notebook_count"] = 1
        journal.metadata["inventory_enabled_keyword_zh"] = ["测试甲"]
        journal.metadata["eligible_legacy_candidates"] = 2
        journal.save()

        pages_dir = (
            iso_discovery.settings.DISCOVERY_DIR / "legacy_archive" / mid / "pending_pages"
        )
        TestExtractCandidates._write_legacy_page(
            pages_dir, "kw_a/p1.json", keyword_id_value=kid, keyword_zh="测试甲",
            candidates=[
                TestExtractCandidates._cand("10.5555/one", "c1"),
                TestExtractCandidates._cand("10.5555/two", "c2"),
            ],
            page_id="p" + "1" * 31,
        )
        monkeypatch.setattr(discover_mod, "main_internal", lambda argv: 0)

        real_write = PendingCandidateStoreV4.write
        calls = {"n": 0}

        def flaky_write(self, candidate):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("simulated crash mid candidate write")
            return real_write(self, candidate)

        monkeypatch.setattr(PendingCandidateStoreV4, "write", flaky_write)
        with pytest.raises(RuntimeError, match="simulated crash"):
            migrate_mod.main(["--resume", mid])

        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.NOTEBOOKS_STAGED
        store = PendingCandidateStoreV4(staging_ws)
        assert store.count() == 1

        # Resume re-runs the step: the first candidate's rewrite is an
        # idempotent create-if-absent hit and conservation still holds.
        monkeypatch.setattr(PendingCandidateStoreV4, "write", real_write)
        assert migrate_mod.main(["--resume", mid]) == 0
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.SMOKE_PASSED
        store = PendingCandidateStoreV4(staging_ws)
        assert store.count() == 2
        stats = journal.metadata["candidate_stats"]
        assert stats["candidates_observed"] == 2
        assert stats["imported"] == 2
        assert stats["quarantined"] == 0
        assert stats["candidates_observed"] == (
            stats["invalid_doi"] + stats["already_existing"]
            + stats["duplicate_seeds"] + stats["imported"] + stats["terminal"]
            + stats["quarantined"] + stats["unresolved"]
        )

    def test_preflight_blocks_quarantined_candidates(
        self, iso_discovery, monkeypatch
    ):
        """A quarantine must block the migration before cutover (Phase 6)."""
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "quarantine-blocks-preflight"
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
        journal.metadata["notebooks_failed"] = 0
        nb = _ready_notebook("测试甲")
        kid = nb["keyword_id"]
        _write_notebook(staging_ws.keyword_notebook_dir, nb)
        journal.metadata["inventory_enabled_notebook_count"] = 1
        journal.metadata["inventory_enabled_keyword_zh"] = ["测试甲"]
        journal.metadata["eligible_legacy_candidates"] = 2
        journal.save()

        pages_dir = (
            iso_discovery.settings.DISCOVERY_DIR / "legacy_archive" / mid / "pending_pages"
        )
        TestExtractCandidates._write_legacy_page(
            pages_dir, "kw_a/p1.json", keyword_id_value=kid, keyword_zh="测试甲",
            candidates=[TestExtractCandidates._cand("10.5555/one", "c1")],
            page_id="p" + "1" * 31,
        )
        TestExtractCandidates._write_legacy_page(
            pages_dir, "kw_x/p2.json", keyword_id_value="z" * 16, keyword_zh="不存在",
            candidates=[TestExtractCandidates._cand("10.5555/two", "c2")],
            page_id="p" + "2" * 31,
        )
        monkeypatch.setattr(discover_mod, "main_internal", lambda argv: 0)

        # Extraction succeeds and quarantines the unattributable candidate,
        # but preflight must refuse to advance while quarantined > 0.
        rc = migrate_mod.main(["--resume", mid])
        assert rc == 1
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.CANDIDATES_EXTRACTED
        stats = journal.metadata["candidate_stats"]
        assert stats["quarantined"] == 1
        assert stats["imported"] == 1
        quarantine = (
            iso_discovery.settings.DISCOVERY_MIGRATIONS_DIR
            / f"{mid}.candidate_quarantine.jsonl"
        )
        assert quarantine.is_file()

    def test_crash_after_preflight_save_resume_completes(
        self, iso_discovery, monkeypatch
    ):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "crash-preflight"
        self._mock_steps(iso_discovery, monkeypatch)

        original_save = journal_mod.MigrationJournal.save
        crashed = {"done": False}

        def flaky_save(self):
            if (
                self.state == journal_mod.MigrationState.PREFLIGHT_VALIDATED
                and not crashed["done"]
            ):
                crashed["done"] = True
                raise RuntimeError("simulated crash after preflight")
            return original_save(self)

        monkeypatch.setattr(journal_mod.MigrationJournal, "save", flaky_save)
        with pytest.raises(RuntimeError, match="simulated crash"):
            migrate_mod.main(["--apply", "--migration-id", mid])
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.CANDIDATES_EXTRACTED

        assert migrate_mod.main(["--resume", mid]) == 0
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.SMOKE_PASSED

    def test_crash_after_smoke_passed_save_resume_reruns_smoke(
        self, iso_discovery, monkeypatch
    ):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "crash-smoke-save"
        self._mock_steps(iso_discovery, monkeypatch)

        smoke_calls = {"n": 0}

        def counting_smoke(argv):
            smoke_calls["n"] += 1
            return 0

        monkeypatch.setattr(discover_mod, "main_internal", counting_smoke)

        original_save = journal_mod.MigrationJournal.save
        crashed = {"done": False}

        def flaky_save(self):
            if (
                self.state == journal_mod.MigrationState.SMOKE_PASSED
                and not crashed["done"]
            ):
                crashed["done"] = True
                raise RuntimeError("simulated crash after smoke passed")
            return original_save(self)

        monkeypatch.setattr(journal_mod.MigrationJournal, "save", flaky_save)
        with pytest.raises(RuntimeError, match="simulated crash"):
            migrate_mod.main(["--apply", "--migration-id", mid])
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.PREFLIGHT_VALIDATED

        assert migrate_mod.main(["--resume", mid]) == 0
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.SMOKE_PASSED
        # The smoke run is side-effect free, so re-running it is safe.
        assert smoke_calls["n"] == 2


class TestInventoryArchiveBinding:
    """The archive step is hash-bound to the inventory closure."""

    def test_source_tampered_between_inventory_and_archive_fails_closed(
        self, iso_discovery, monkeypatch, capsys
    ):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "bind-tamper"
        page = TestApplyCrashResumeIdempotency._seed_real_page(iso_discovery)
        original = page.read_bytes()
        real_prepare = migrate_mod.prepare_legacy_archive

        def tampering_prepare(migration_id: str):
            page.write_text("tampered-after-inventory", encoding="utf-8")
            try:
                return real_prepare(migration_id)
            finally:
                page.write_bytes(original)

        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report",
            lambda **_: _inventory_report_with_real_pages(iso_discovery, _FAKE_KEYWORDS),
        )
        monkeypatch.setattr(migrate_mod, "prepare_legacy_archive", tampering_prepare)
        monkeypatch.setattr(migrate_mod, "migrate_all_notebooks", _fake_migrate_all_notebooks)
        monkeypatch.setattr(discover_mod, "main_internal", lambda argv: 0)

        rc = migrate_mod.main(["--apply", "--migration-id", mid])
        assert rc == 1
        err = capsys.readouterr().err
        assert "does not match the inventory closure" in err
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.INVENTORY_COMPLETE

        # The tampered leftover archive fails the binding on resume too; it
        # is discarded and rebuilt from the (restored) source.
        monkeypatch.setattr(migrate_mod, "prepare_legacy_archive", real_prepare)
        assert migrate_mod.main(["--resume", mid]) == 0
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.SMOKE_PASSED

    def test_journal_without_binding_metadata_skips_check(
        self, iso_discovery, capsys
    ):
        migrate_mod = iso_discovery.migrate_mod
        journal_mod = iso_discovery.journal_mod
        mid = "bind-legacy-journal"
        TestApplyCrashResumeIdempotency._seed_real_page(iso_discovery)
        journal = journal_mod.MigrationJournal.create(mid)
        journal.transition_to(journal_mod.MigrationState.INVENTORY_COMPLETE)
        journal.save()

        args = migrate_mod._parse_args(["--apply"])
        result = migrate_mod._step_archive(journal, args)
        assert result["pending_pages_total"] == 1
        out = capsys.readouterr().out
        assert "binding not enforced" in out
        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.ARCHIVE_PREPARED


class TestPlanReadOnly:
    def test_plan_writes_nothing(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report",
            lambda **_: _minimal_inventory_report(_FAKE_KEYWORDS),
        )
        discovery_dir = iso_discovery.settings.DISCOVERY_DIR
        before = {p.relative_to(discovery_dir) for p in discovery_dir.rglob("*")}
        rc = migrate_mod.main(["--plan"])
        assert rc == 0
        after = {p.relative_to(discovery_dir) for p in discovery_dir.rglob("*")}
        assert before == after

    def test_plan_output_writes_atomically(self, iso_discovery, monkeypatch, tmp_path):
        migrate_mod = iso_discovery.migrate_mod
        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report",
            lambda **_: _minimal_inventory_report(_FAKE_KEYWORDS),
        )
        out_path = tmp_path / "plan-out" / "plan.json"
        rc = migrate_mod.main(["--plan", "--plan-output", str(out_path)])
        assert rc == 0
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert data["plan_type"] == "discovery_v4_migration"
        assert data["expected_lanes"] == 24
        assert not (out_path.parent / "plan.json.tmp").exists()
        # The discovery tree itself is still untouched.
        discovery_dir = iso_discovery.settings.DISCOVERY_DIR
        assert not (discovery_dir / "migrations" / "v4_migration_plan.json").exists()


class TestDryRunExitCodes:
    def test_notebook_failure_returns_nonzero(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report",
            lambda **_: _minimal_inventory_report(_FAKE_KEYWORDS),
        )
        monkeypatch.setattr(
            migrate_mod, "migrate_all_notebooks",
            lambda notebook_dir, output_dir: [
                {"success": False, "keyword_zh": "坏笔记本", "error": "boom"},
            ],
        )
        rc = migrate_mod.main(["--dry-run", "--migration-id", "dry-fail-nb"])
        assert rc == 1
        assert not (
            iso_discovery.settings.DISCOVERY_MIGRATIONS_DIR / "dry-fail-nb.json"
        ).exists()

    def test_corrupt_legacy_journal_returns_nonzero(self, iso_discovery, monkeypatch):
        migrate_mod = iso_discovery.migrate_mod
        monkeypatch.setattr(
            migrate_mod, "generate_inventory_report",
            lambda **_: _minimal_inventory_report(_FAKE_KEYWORDS),
        )
        monkeypatch.setattr(
            migrate_mod, "migrate_all_notebooks",
            lambda notebook_dir, output_dir: [
                {"success": True, "keyword_zh": "测试", "active_queries": 1, "lane_count": 4},
            ],
        )
        corrupt = (
            iso_discovery.settings.DISCOVERY_PENDING_PAGES_DIR / "kw" / "corrupt.json"
        )
        corrupt.parent.mkdir(parents=True, exist_ok=True)
        corrupt.write_text("{not json", encoding="utf-8")

        rc = migrate_mod.main(["--dry-run", "--migration-id", "dry-fail-conservation"])
        assert rc == 1
        assert not (
            iso_discovery.settings.DISCOVERY_MIGRATIONS_DIR
            / "dry-fail-conservation.json"
        ).exists()
