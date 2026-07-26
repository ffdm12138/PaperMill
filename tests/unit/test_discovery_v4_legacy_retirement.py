"""Unit tests for discovery v4 legacy-source retirement (Phase 4).

All tests run under tmp_path; no real runtime data is touched.
"""
from __future__ import annotations

import importlib
import json
import os
import threading
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from filelock import FileLock

import src.migrations.discovery_v4.legacy_retirement as lr
from src.migrations.discovery_v4.legacy_retirement import (
    LegacyRetirementError,
    compute_directory_manifest,
    purge_retained_legacy,
    retire_legacy_sources,
)

MID = "v4-20990101-abcdef01-testmid1"


def _write_tree(root: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _flat_files() -> tuple[dict[str, bytes], dict[str, bytes]]:
    notebooks = {
        "积雪__aaaa1111.json": b'{"keyword_zh": "a"}',
        "冰川__bbbb2222.json": b'{"keyword_zh": "b"}',
    }
    pages = {
        "pages/page_0001.json": b'{"records": [1]}',
        "pages/nested/page_0002.json": b'{"records": [2]}',
    }
    return notebooks, pages


@pytest.fixture
def legacy_env(tmp_path):
    """Flat legacy dirs + clean reconciliation report + matching receipts."""
    discovery = tmp_path / "discovery"
    flat_nb = discovery / "keyword_notebooks"
    flat_pp = discovery / "pending_pages"
    nb_files, pp_files = _flat_files()
    _write_tree(flat_nb, nb_files)
    _write_tree(flat_pp, pp_files)

    migrations = discovery / "migrations"
    migrations.mkdir(parents=True)
    report_path = migrations / f"{MID}.post_cutover_reconciliation.json"
    report_path.write_text(json.dumps({
        "schema_version": "1.0",
        "migration_id": MID,
        "unresolved_items": 0,
        "receipts_verified": 3,
    }), encoding="utf-8")
    receipts = migrations / f"{MID}.receipts"
    receipts.mkdir()
    for i in range(3):
        (receipts / f"{i:032x}.json").write_text("{}", encoding="utf-8")

    return types.SimpleNamespace(
        tmp_path=tmp_path,
        flat_nb=flat_nb,
        flat_pp=flat_pp,
        retained_root=discovery / "legacy_retained",
        report_path=report_path,
        receipts_dir=receipts,
        nb_files=nb_files,
        pp_files=pp_files,
    )


def _retire(env, **overrides):
    kwargs = dict(
        migration_id=MID,
        flat_notebooks_dir=env.flat_nb,
        flat_pending_pages_dir=env.flat_pp,
        retained_root=env.retained_root,
        reconciliation_report_path=env.report_path,
    )
    kwargs.update(overrides)
    return retire_legacy_sources(**kwargs)


def _write_active_pointer(path: Path, migration_id: str) -> None:
    path.write_text(json.dumps({
        "schema_version": "4.0",
        "generation_id": "gen-test",
        "workspace_manifest_sha256": "0" * 64,
        "activated_at": "2099-01-01T00:00:00+00:00",
        "migration_id": migration_id,
    }), encoding="utf-8")


class TestComputeDirectoryManifest:
    def test_deterministic_and_order_independent(self, tmp_path):
        files = {"b/two.txt": b"two", "a/one.txt": b"one", "three.txt": b""}
        tree_a = tmp_path / "a"
        tree_b = tmp_path / "b"
        _write_tree(tree_a, files)
        # Same content, written in reverse order.
        _write_tree(tree_b, dict(reversed(list(files.items()))))
        m_a = compute_directory_manifest(tree_a)
        m_b = compute_directory_manifest(tree_b)
        assert m_a["aggregate_sha256"] == m_b["aggregate_sha256"]
        assert m_a["file_count"] == 3
        assert m_a["total_bytes"] == 6
        # Recompute is stable.
        assert compute_directory_manifest(tree_a)["aggregate_sha256"] == \
            m_a["aggregate_sha256"]

    def test_content_change_changes_aggregate(self, tmp_path):
        tree = tmp_path / "t"
        _write_tree(tree, {"f.txt": b"one"})
        before = compute_directory_manifest(tree)["aggregate_sha256"]
        (tree / "f.txt").write_bytes(b"two")
        assert compute_directory_manifest(tree)["aggregate_sha256"] != before

    def test_missing_root_fails_closed(self, tmp_path):
        with pytest.raises(LegacyRetirementError):
            compute_directory_manifest(tmp_path / "missing")


class TestRetireHappyPath:
    def test_moves_verifies_tombstones(self, legacy_env):
        env = legacy_env
        result = _retire(env)

        # Flat dirs are no longer directories; the retained tree holds every file.
        assert not env.flat_nb.is_dir()
        assert not env.flat_pp.is_dir()
        base = env.retained_root / MID
        for rel in env.nb_files:
            assert (base / "keyword_notebooks" / rel).is_file()
        for rel in env.pp_files:
            assert (base / "pending_pages" / rel).is_file()

        # Result manifest summaries match a fresh recompute.
        for name in ("keyword_notebooks", "pending_pages"):
            fresh = compute_directory_manifest(base / name)
            summary = result["manifests"][name]
            assert summary["aggregate_sha256"] == fresh["aggregate_sha256"]
            assert summary["file_count"] == fresh["file_count"]
            assert summary["total_bytes"] == fresh["total_bytes"]

        # Retention manifest closes over both trees and the window.
        manifest = json.loads(
            (base / "retention_manifest.json").read_text(encoding="utf-8"))
        assert manifest["schema_version"] == "1.0"
        assert manifest["migration_id"] == MID
        assert manifest["retention_days"] == 90
        retired_at = datetime.fromisoformat(manifest["retired_at"])
        purge_not_before = datetime.fromisoformat(manifest["purge_not_before"])
        assert purge_not_before - retired_at == timedelta(days=90)
        assert manifest["source_paths"]["keyword_notebooks"] == str(env.flat_nb)
        assert manifest["manifests"]["pending_pages"]["file_count"] == 2

        # Retained tree is read-only.
        sample = base / "keyword_notebooks" / next(iter(env.nb_files))
        assert not os.access(sample, os.W_OK)

        # Tombstones are FILES at the original flat paths.
        for original in (env.flat_nb, env.flat_pp):
            assert original.is_file()
            tombstone = json.loads(original.read_text(encoding="utf-8"))
            assert tombstone["retired"] is True
            assert tombstone["migration_id"] == MID
            with pytest.raises(FileExistsError):
                original.mkdir(exist_ok=True)


class TestRetireGates:
    def test_missing_report(self, legacy_env):
        legacy_env.report_path.unlink()
        with pytest.raises(LegacyRetirementError, match="report missing"):
            _retire(legacy_env)
        self._assert_untouched(legacy_env)

    def test_unresolved_items_nonzero(self, legacy_env):
        report = json.loads(legacy_env.report_path.read_text(encoding="utf-8"))
        report["unresolved_items"] = 2
        legacy_env.report_path.write_text(json.dumps(report), encoding="utf-8")
        with pytest.raises(LegacyRetirementError, match="unresolved_items"):
            _retire(legacy_env)
        self._assert_untouched(legacy_env)

    def test_migration_id_mismatch(self, legacy_env):
        with pytest.raises(LegacyRetirementError, match="does not match"):
            _retire(legacy_env, migration_id="v4-other-migration")
        self._assert_untouched(legacy_env)

    def test_receipts_dir_missing(self, legacy_env):
        import shutil
        shutil.rmtree(legacy_env.receipts_dir)
        with pytest.raises(LegacyRetirementError, match="receipts directory missing"):
            _retire(legacy_env)
        self._assert_untouched(legacy_env)

    def test_receipt_count_mismatch(self, legacy_env):
        next(legacy_env.receipts_dir.iterdir()).unlink()
        with pytest.raises(LegacyRetirementError, match="receipt count mismatch"):
            _retire(legacy_env)
        self._assert_untouched(legacy_env)

    def test_retained_target_exists(self, legacy_env):
        (legacy_env.retained_root / MID).mkdir(parents=True)
        with pytest.raises(LegacyRetirementError, match="already exists"):
            _retire(legacy_env)
        assert legacy_env.flat_nb.is_dir()
        assert legacy_env.flat_pp.is_dir()

    def test_flat_dir_missing(self, legacy_env):
        import shutil
        shutil.rmtree(legacy_env.flat_pp)
        with pytest.raises(LegacyRetirementError, match="flat directory missing"):
            _retire(legacy_env)
        assert legacy_env.flat_nb.is_dir()

    def _assert_untouched(self, env) -> None:
        assert env.flat_nb.is_dir()
        assert env.flat_pp.is_dir()
        assert not env.retained_root.exists()


class TestRetireVerificationFailure:
    def test_manifest_mismatch_leaves_moved_data_in_place(
        self, legacy_env, monkeypatch
    ):
        original = lr.compute_directory_manifest
        calls = {"n": 0}

        def tampered(root):
            calls["n"] += 1
            result = original(root)
            if calls["n"] == 3:
                # First destination recompute: fake a hash mismatch.
                result = dict(result, aggregate_sha256="0" * 64)
            return result

        monkeypatch.setattr(lr, "compute_directory_manifest", tampered)
        with pytest.raises(LegacyRetirementError, match="manifest mismatch"):
            _retire(legacy_env)
        # Both moves happened before verification; data is left in place and
        # no tombstones mask the original paths.
        assert (legacy_env.retained_root / MID / "keyword_notebooks").is_dir()
        assert (legacy_env.retained_root / MID / "pending_pages").is_dir()
        assert not legacy_env.flat_nb.exists()
        assert not legacy_env.flat_pp.exists()


class TestPurgeRetainedLegacy:
    def _retired(self, legacy_env, retention_days=1):
        result = _retire(legacy_env, retention_days=retention_days)
        pointer = legacy_env.tmp_path / "active_generation.json"
        _write_active_pointer(pointer, MID)
        return result, pointer

    def test_purge_after_retention_window(self, legacy_env):
        env = legacy_env
        result, pointer = self._retired(env)
        purge_not_before = datetime.fromisoformat(result["purge_not_before"])
        now = purge_not_before + timedelta(seconds=1)
        summary = purge_retained_legacy(
            migration_id=MID,
            retained_root=env.retained_root,
            confirm_migration_id=MID,
            now=now,
            active_generation_path=pointer,
        )
        assert not (env.retained_root / MID).exists()
        assert not env.flat_nb.exists()
        assert not env.flat_pp.exists()
        assert summary["migration_id"] == MID
        assert len(summary["tombstones_removed"]) == 2

    def test_refuses_before_retention_date(self, legacy_env):
        env = legacy_env
        result, pointer = self._retired(env)
        purge_not_before = datetime.fromisoformat(result["purge_not_before"])
        with pytest.raises(LegacyRetirementError, match="retention window"):
            purge_retained_legacy(
                migration_id=MID,
                retained_root=env.retained_root,
                confirm_migration_id=MID,
                now=purge_not_before - timedelta(seconds=1),
                active_generation_path=pointer,
            )
        assert (env.retained_root / MID).is_dir()

    def test_refuses_on_confirm_mismatch(self, legacy_env):
        env = legacy_env
        result, pointer = self._retired(env)
        with pytest.raises(LegacyRetirementError, match="confirm"):
            purge_retained_legacy(
                migration_id=MID,
                retained_root=env.retained_root,
                confirm_migration_id="v4-something-else",
                now=datetime.fromisoformat(result["purge_not_before"]),
                active_generation_path=pointer,
            )
        assert (env.retained_root / MID).is_dir()

    def test_refuses_on_missing_manifest(self, legacy_env):
        env = legacy_env
        (env.retained_root / MID).mkdir(parents=True)
        pointer = env.tmp_path / "active_generation.json"
        _write_active_pointer(pointer, MID)
        with pytest.raises(LegacyRetirementError, match="retention manifest missing"):
            purge_retained_legacy(
                migration_id=MID,
                retained_root=env.retained_root,
                confirm_migration_id=MID,
                now=datetime.now(timezone.utc),
                active_generation_path=pointer,
            )

    def test_refuses_on_active_pointer_mismatch(self, legacy_env):
        env = legacy_env
        result, pointer = self._retired(env)
        _write_active_pointer(pointer, "v4-different-migration")
        with pytest.raises(LegacyRetirementError, match="active generation pointer"):
            purge_retained_legacy(
                migration_id=MID,
                retained_root=env.retained_root,
                confirm_migration_id=MID,
                now=datetime.fromisoformat(result["purge_not_before"]),
                active_generation_path=pointer,
            )
        assert (env.retained_root / MID).is_dir()

    def test_refuses_to_remove_foreign_tombstone(self, legacy_env):
        env = legacy_env
        result, pointer = self._retired(env)
        env.flat_nb.write_text(json.dumps({
            "schema_version": "1.0",
            "retired": True,
            "migration_id": "v4-another-migration",
        }), encoding="utf-8")
        with pytest.raises(LegacyRetirementError, match="tombstone"):
            purge_retained_legacy(
                migration_id=MID,
                retained_root=env.retained_root,
                confirm_migration_id=MID,
                now=datetime.fromisoformat(result["purge_not_before"]),
                active_generation_path=pointer,
            )
        assert (env.retained_root / MID).is_dir()


# ── CLI-level tests ─────────────────────────────────────────────────────────


@pytest.fixture
def iso_cli(tmp_path, monkeypatch):
    """Isolated discovery dirs with patched settings + reloaded CLI modules."""
    import config.settings as settings
    import src.discovery.workspace as workspace_mod
    import src.migrations.discovery_v4.archive_builder as archive_mod
    import src.migrations.discovery_v4.candidate_extraction as candidate_mod
    import src.migrations.discovery_v4.migration_journal as journal_mod

    discovery_dir = tmp_path / "discovery"
    migrations_dir = discovery_dir / "migrations"
    generations_dir = discovery_dir / "generations"
    staging_dir = generations_dir / ".staging"
    keyword_notebook_dir = discovery_dir / "keyword_notebooks"
    pending_pages_dir = discovery_dir / "pending_pages"
    for d in [
        discovery_dir, migrations_dir, generations_dir, staging_dir,
        keyword_notebook_dir, pending_pages_dir,
    ]:
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(settings, "DISCOVERY_DIR", discovery_dir)
    monkeypatch.setattr(settings, "DISCOVERY_MIGRATIONS_DIR", migrations_dir)
    monkeypatch.setattr(settings, "DISCOVERY_LEGACY_ARCHIVE_DIR",
                        discovery_dir / "legacy_archive")
    monkeypatch.setattr(settings, "DISCOVERY_GENERATIONS_DIR", generations_dir)
    monkeypatch.setattr(settings, "DISCOVERY_STAGING_DIR", staging_dir)
    monkeypatch.setattr(settings, "DISCOVERY_ACTIVE_GENERATION_PATH",
                        discovery_dir / "active_generation.json")
    monkeypatch.setattr(settings, "DISCOVERY_KEYWORD_NOTEBOOK_DIR", keyword_notebook_dir)
    monkeypatch.setattr(settings, "DISCOVERY_PENDING_PAGES_DIR", pending_pages_dir)

    importlib.reload(workspace_mod)
    importlib.reload(archive_mod)
    importlib.reload(candidate_mod)
    importlib.reload(journal_mod)
    import scripts.migrate_discovery_v4 as migrate_mod
    importlib.reload(migrate_mod)

    yield types.SimpleNamespace(
        tmp_path=tmp_path,
        settings=settings,
        migrate_mod=migrate_mod,
        journal_mod=journal_mod,
    )

    # Reload the touched modules back against the real settings so later
    # tests in the same process are not polluted by this fixture.
    monkeypatch.undo()
    importlib.reload(workspace_mod)
    importlib.reload(archive_mod)
    importlib.reload(candidate_mod)
    importlib.reload(journal_mod)
    importlib.reload(migrate_mod)


def _finalized_journal(iso_cli, mid: str):
    journal = iso_cli.journal_mod.MigrationJournal.create(migration_id=mid)
    journal.state = iso_cli.journal_mod.MigrationState.FINALIZED
    journal.save()
    return journal


def _prepare_retire_inputs(iso_cli, mid: str) -> None:
    settings = iso_cli.settings
    nb_files, pp_files = _flat_files()
    _write_tree(settings.DISCOVERY_KEYWORD_NOTEBOOK_DIR, nb_files)
    _write_tree(settings.DISCOVERY_PENDING_PAGES_DIR, pp_files)
    report_path = (
        settings.DISCOVERY_MIGRATIONS_DIR
        / f"{mid}.post_cutover_reconciliation.json"
    )
    report_path.write_text(json.dumps({
        "schema_version": "1.0",
        "migration_id": mid,
        "unresolved_items": 0,
        "receipts_verified": 1,
    }), encoding="utf-8")
    receipts = settings.DISCOVERY_MIGRATIONS_DIR / f"{mid}.receipts"
    receipts.mkdir()
    (receipts / ("0" * 32 + ".json")).write_text("{}", encoding="utf-8")


def _run_in_thread(fn) -> dict:
    results: dict[str, int] = {}
    worker = threading.Thread(target=lambda: results.setdefault("rc", fn()))
    worker.start()
    worker.join(timeout=60)
    assert not worker.is_alive()
    return results


class TestRetireLegacySourcesCLI:
    def test_success_updates_journal_metadata(self, iso_cli):
        migrate_mod = iso_cli.migrate_mod
        journal_mod = iso_cli.journal_mod
        settings = iso_cli.settings
        mid = "v4-cli-retire-ok"
        _finalized_journal(iso_cli, mid)
        _prepare_retire_inputs(iso_cli, mid)

        assert migrate_mod.main(["--retire-legacy-sources", mid]) == 0

        journal = journal_mod.MigrationJournal.load(mid)
        assert journal.state == journal_mod.MigrationState.FINALIZED
        entry = journal.metadata.get("legacy_retirement")
        assert entry is not None
        assert entry["migration_id"] == mid
        retained = settings.DISCOVERY_DIR / "legacy_retained" / mid
        assert (retained / "keyword_notebooks").is_dir()
        assert (retained / "pending_pages").is_dir()
        # Tombstones are files at the original flat paths.
        assert settings.DISCOVERY_KEYWORD_NOTEBOOK_DIR.is_file()
        assert settings.DISCOVERY_PENDING_PAGES_DIR.is_file()

    def test_fails_closed_while_maintenance_lock_held(self, iso_cli):
        migrate_mod = iso_cli.migrate_mod
        journal_mod = iso_cli.journal_mod
        settings = iso_cli.settings
        mid = "v4-cli-retire-lock"
        _finalized_journal(iso_cli, mid)
        _prepare_retire_inputs(iso_cli, mid)

        lock_path = settings.DISCOVERY_MIGRATIONS_DIR / ".migration.lock"
        held = FileLock(str(lock_path), timeout=0)
        with held:
            results = _run_in_thread(
                lambda: migrate_mod.main(["--retire-legacy-sources", mid])
            )
        assert results["rc"] == 1
        # Nothing moved while the lock was held.
        assert settings.DISCOVERY_KEYWORD_NOTEBOOK_DIR.is_dir()
        assert settings.DISCOVERY_PENDING_PAGES_DIR.is_dir()
        assert not (settings.DISCOVERY_DIR / "legacy_retained").exists()
        journal = journal_mod.MigrationJournal.load(mid)
        assert "legacy_retirement" not in journal.metadata

        # After release the command proceeds.
        assert migrate_mod.main(["--retire-legacy-sources", mid]) == 0
        journal = journal_mod.MigrationJournal.load(mid)
        assert "legacy_retirement" in journal.metadata

    def test_refuses_non_finalized_journal(self, iso_cli):
        migrate_mod = iso_cli.migrate_mod
        journal_mod = iso_cli.journal_mod
        mid = "v4-cli-retire-state"
        journal_mod.MigrationJournal.create(migration_id=mid)  # planned
        _prepare_retire_inputs(iso_cli, mid)
        assert migrate_mod.main(["--retire-legacy-sources", mid]) == 1
        journal = journal_mod.MigrationJournal.load(mid)
        assert "legacy_retirement" not in journal.metadata


class TestPurgeRetainedLegacyCLI:
    def test_requires_confirm_migration_id(self, iso_cli):
        migrate_mod = iso_cli.migrate_mod
        mid = "v4-cli-purge-confirm"
        _finalized_journal(iso_cli, mid)
        assert migrate_mod.main(["--purge-retained-legacy", mid]) == 1

    def test_fails_closed_while_maintenance_lock_held(self, iso_cli):
        migrate_mod = iso_cli.migrate_mod
        settings = iso_cli.settings
        mid = "v4-cli-purge-lock"
        _finalized_journal(iso_cli, mid)
        lock_path = settings.DISCOVERY_MIGRATIONS_DIR / ".migration.lock"
        held = FileLock(str(lock_path), timeout=0)
        with held:
            results = _run_in_thread(
                lambda: migrate_mod.main([
                    "--purge-retained-legacy", mid,
                    "--confirm-migration-id", mid,
                ])
            )
        assert results["rc"] == 1


class TestSettingsEnsureDirs:
    def test_import_does_not_create_legacy_flat_dirs(self, tmp_path, monkeypatch):
        import config.settings as settings

        monkeypatch.setenv("MINERU_DATA_DIR", str(tmp_path / "data"))
        importlib.reload(settings)
        try:
            assert settings.DISCOVERY_DIR.is_dir()
            assert settings.DISCOVERY_LOCKS_DIR.is_dir()
            assert not settings.DISCOVERY_KEYWORD_NOTEBOOK_DIR.exists()
            assert not settings.DISCOVERY_PENDING_PAGES_DIR.exists()
        finally:
            monkeypatch.undo()
            # Reload again so the module reflects the real environment for
            # subsequent tests in the same process.
            importlib.reload(settings)
            importlib.reload(settings)
