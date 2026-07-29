"""Unit tests for v4 DiscoveryWorkspace."""
from __future__ import annotations

import hashlib
import json
import pytest
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

from filelock import FileLock

from src.discovery.contracts.manifest import (
    STORE_SCHEMA_VERSIONS_V4,
    ActiveGenerationPointerV4,
    DiscoveryWorkspaceManifestV4,
)
from src.discovery.workspace import (
    DiscoveryWorkspace,
    WorkspaceResolver,
    create_staging_workspace,
)

_ACTIVATED_AT = "2026-01-01T00:00:00+00:00"


def _pointer(
    generation_id: str,
    manifest_bytes: bytes,
    migration_id: str = "mig-1",
    **kwargs,
) -> ActiveGenerationPointerV4:
    return ActiveGenerationPointerV4(
        generation_id=generation_id,
        workspace_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        activated_at=_ACTIVATED_AT,
        migration_id=migration_id,
        **kwargs,
    )


class TestDiscoveryWorkspaceConstruction:
    """Test DiscoveryWorkspace.from_generation_id()."""

    def test_from_generation_id_basic(self):
        ws = DiscoveryWorkspace.from_generation_id("v4-test-1")
        assert ws.generation_id == "v4-test-1"
        assert "generations" in str(ws.root)
        assert ws.root.name == "v4-test-1"

    def test_from_generation_id_sets_all_subdirs(self):
        ws = DiscoveryWorkspace.from_generation_id("v4-test-2")
        # KNOWN_SUBDIRS uses directory-style names (e.g., "keyword_notebooks")
        # class attributes use singular (e.g., "keyword_notebook_dir")
        attr_names = [
            "keyword_notebook_dir", "page_journals_dir",
            "exports_dir", "reports_dir", "locks_dir",
        ]
        for attr in attr_names:
            d = getattr(ws, attr)
            assert isinstance(d, Path), f"{attr} is not Path: {d!r}"
            assert d.parent == ws.root, f"{attr} parent mismatch: {d.parent} != {ws.root}"

    def test_rejects_empty_id(self):
        try:
            DiscoveryWorkspace.from_generation_id("")
            assert False, "should have raised ValueError"
        except ValueError:
            pass

    def test_rejects_id_with_slash(self):
        try:
            DiscoveryWorkspace.from_generation_id("bad/id")
            assert False, "should have raised ValueError"
        except ValueError:
            pass

    def test_rejects_id_with_backslash(self):
        try:
            DiscoveryWorkspace.from_generation_id("bad\\id")
            assert False, "should have raised ValueError"
        except ValueError:
            pass

    def test_rejects_id_with_newline(self):
        try:
            DiscoveryWorkspace.from_generation_id("bad\nid")
            assert False, "should have raised ValueError"
        except ValueError:
            pass


class TestDiscoveryWorkspaceEnsureDirs:
    """Test ensure_dirs() creates all subdirectories."""

    def test_ensure_dirs_creates_all(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            root = tmp / "test-g1"
            ws = DiscoveryWorkspace(
                generation_id="test-g1",
                root=root,
                keyword_notebook_dir=root / "keyword_notebooks",
                page_journals_dir=root / "page_journals",
                exports_dir=root / "exports",
                reports_dir=root / "reports",
                locks_dir=root / "locks",
            )
            ws.ensure_dirs()
            dir_attrs = [
                ws.keyword_notebook_dir,
                ws.page_journals_dir,
                ws.exports_dir, ws.reports_dir, ws.locks_dir,
            ]
            for d in dir_attrs:
                assert d.is_dir(), f"not created: {d}"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ensure_dirs_idempotent(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            root = tmp / "test-g2"
            ws = DiscoveryWorkspace(
                generation_id="test-g2",
                root=root,
                keyword_notebook_dir=root / "keyword_notebooks",
                page_journals_dir=root / "page_journals",
                exports_dir=root / "exports",
                reports_dir=root / "reports",
                locks_dir=root / "locks",
            )
            ws.ensure_dirs()
            ws.ensure_dirs()  # second call should not raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_verify_dirs_ignores_leftover_pending_candidates(self):
        """A retired generation may still carry the finalized migration's
        pending_candidates directory and the deleted dead-stack lane_states /
        indexes directories on disk; none are part of the layout and must not
        affect verification."""
        tmp = Path(tempfile.mkdtemp())
        try:
            root = tmp / "test-g3"
            ws = DiscoveryWorkspace(
                generation_id="test-g3",
                root=root,
                keyword_notebook_dir=root / "keyword_notebooks",
                page_journals_dir=root / "page_journals",
                exports_dir=root / "exports",
                reports_dir=root / "reports",
                locks_dir=root / "locks",
            )
            ws.ensure_dirs()
            (root / "pending_candidates").mkdir()
            (root / "lane_states").mkdir()
            (root / "indexes").mkdir()
            assert ws.verify_dirs() == []
            # Required directories are still enforced.
            shutil.rmtree(ws.reports_dir)
            assert ws.verify_dirs() == ["reports"]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestStagingWorkspace:
    """Test staging workspace creation."""

    def test_create_staging_workspace_basic(self, tmp_path):
        import src.discovery.workspace as wsp
        original = wsp.STAGING_DIR
        try:
            wsp.STAGING_DIR = tmp_path / ".staging"
            ws = create_staging_workspace("staging-test-1")
            assert ws.generation_id == "staging-test-1"
            assert ws.root.is_dir()
        finally:
            wsp.STAGING_DIR = original
            shutil.rmtree(tmp_path, ignore_errors=True)

    def test_create_staging_duplicate_fails(self, tmp_path):
        import src.discovery.workspace as wsp
        original = wsp.STAGING_DIR
        try:
            wsp.STAGING_DIR = tmp_path / ".staging"
            create_staging_workspace("dup-test")
            try:
                create_staging_workspace("dup-test")
                assert False, "should have raised FileExistsError"
            except FileExistsError:
                pass
        finally:
            wsp.STAGING_DIR = original
            shutil.rmtree(tmp_path, ignore_errors=True)


class TestResolveActiveWorkspace:
    """Test active workspace resolution — uses WorkspaceResolver exclusively."""

    def test_resolve_active_raises_when_no_pointer(self):
        """WorkspaceResolver.resolve_active() raises when pointer is missing."""
        from src.discovery.workspace import ActiveGenerationMissingError
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            resolver = WorkspaceResolver(
                active_pointer_path=Path(td) / "nonexistent.json",
                generations_root=Path(td) / "generations",
            )
            with pytest.raises(ActiveGenerationMissingError):
                resolver.resolve_active()


class TestResolveActiveTreeVerification:
    """A7: opt-in workspace tree hash verification (migration window only)."""

    @staticmethod
    def _make_active(tmp_path: Path, monkeypatch) -> SimpleNamespace:
        # Import lazily: other test modules reload src.discovery.workspace,
        # and reloaded exception classes fail identity checks against
        # top-level imports.
        import src.discovery.workspace as wsp

        # resolve_active builds the returned workspace through the
        # module-level generations dir; align it with the injected root.
        generations = tmp_path / "generations"
        monkeypatch.setattr(wsp, "DISCOVERY_GENERATIONS_DIR", generations)
        gen_root = generations / "gen-tree"
        for sub in wsp.KNOWN_SUBDIRS:
            (gen_root / sub).mkdir(parents=True, exist_ok=True)
        (gen_root / "keyword_notebooks" / "nb__abcd1234.json").write_text(
            "{}", encoding="utf-8"
        )
        manifest = DiscoveryWorkspaceManifestV4(
            generation_id="gen-tree",
            migration_id="mig-tree",
            created_at=_ACTIVATED_AT,
            completed_at=_ACTIVATED_AT,
            store_schema_versions=dict(STORE_SCHEMA_VERSIONS_V4),
            workspace_tree_sha256=wsp.hash_workspace_tree(
                gen_root, exclude={"workspace.json"}
            ),
        )
        ws_json = gen_root / "workspace.json"
        ws_json.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
        pointer = _pointer(
            "gen-tree", ws_json.read_bytes(), migration_id="mig-tree"
        )
        active_path = tmp_path / "active_generation.json"
        active_path.write_text(json.dumps(pointer.to_dict()), encoding="utf-8")
        return SimpleNamespace(
            resolver=wsp.WorkspaceResolver(
                active_pointer_path=active_path,
                generations_root=generations,
            ),
            gen_root=gen_root,
        )

    def test_production_resolve_passes(self, tmp_path, monkeypatch):
        ns = self._make_active(tmp_path, monkeypatch)
        ws = ns.resolver.resolve_active()
        assert ws.generation_id == "gen-tree"

    def test_verify_tree_passes_on_untouched_closure(self, tmp_path, monkeypatch):
        ns = self._make_active(tmp_path, monkeypatch)
        ws = ns.resolver.resolve_active(verify_tree=True)
        assert ws.generation_id == "gen-tree"

    def test_verify_tree_rejects_tampered_file(self, tmp_path, monkeypatch):
        import src.discovery.workspace as wsp

        ns = self._make_active(tmp_path, monkeypatch)
        nb = ns.gen_root / "keyword_notebooks" / "nb__abcd1234.json"
        nb.write_text('{"tampered": true}', encoding="utf-8")
        with pytest.raises(wsp.WorkspaceManifestMismatchError, match="tree SHA-256"):
            ns.resolver.resolve_active(verify_tree=True)

    def test_production_resolve_not_rejected_by_runtime_writes(self, tmp_path, monkeypatch):
        import src.discovery.workspace as wsp

        ns = self._make_active(tmp_path, monkeypatch)
        # Simulate one normal discovery run: notebooks mutated in place,
        # page journals committed, reports / locks created.
        nb = ns.gen_root / "keyword_notebooks" / "nb__abcd1234.json"
        nb.write_text('{"updated_at": "later"}', encoding="utf-8")
        (ns.gen_root / "page_journals" / "p1.json").write_text(
            "{}", encoding="utf-8"
        )
        (ns.gen_root / "reports" / "run.json").write_text("{}", encoding="utf-8")
        (ns.gen_root / "locks" / "x.lock").write_text("", encoding="utf-8")

        # The production resolve path (every discovery CLI startup) must not
        # be rejected by runtime content drift...
        ws = ns.resolver.resolve_active()
        assert ws.generation_id == "gen-tree"
        # ...while the migration-window check correctly reports the drift.
        with pytest.raises(wsp.WorkspaceManifestMismatchError):
            ns.resolver.resolve_active(verify_tree=True)


class TestActiveGenerationPointerContract:
    """Strict pointer contract, including optional previous_generation_id."""

    def test_roundtrip_without_previous(self):
        pointer = _pointer("gen-a", b"{}")
        parsed = ActiveGenerationPointerV4.from_dict_strict(pointer.to_dict())
        assert parsed == pointer
        assert parsed.previous_generation_id is None
        assert "previous_generation_id" not in pointer.to_dict()

    def test_roundtrip_with_previous(self):
        pointer = _pointer("gen-b", b"{}", previous_generation_id="gen-a")
        parsed = ActiveGenerationPointerV4.from_dict_strict(pointer.to_dict())
        assert parsed == pointer
        assert parsed.previous_generation_id == "gen-a"
        assert pointer.to_dict()["previous_generation_id"] == "gen-a"

    def test_strict_parse_rejects_unknown_fields(self):
        payload = _pointer("gen-c", b"{}").to_dict()
        payload["unexpected"] = "x"
        with pytest.raises(ValueError, match="unknown fields"):
            ActiveGenerationPointerV4.from_dict_strict(payload)

    def test_invalid_previous_generation_id_rejected(self):
        with pytest.raises(ValueError):
            _pointer("gen-d", b"{}", previous_generation_id="bad/id")
        with pytest.raises(ValueError):
            _pointer("gen-d", b"{}", previous_generation_id="  ")


class TestCommitWorkspace:
    """Idempotent, lock-guarded cutover commit (A5)."""

    @staticmethod
    @pytest.fixture
    def iso_ws(tmp_path, monkeypatch):
        import src.discovery.workspace as wsp

        generations = tmp_path / "generations"
        staging = generations / ".staging"
        migrations = tmp_path / "migrations"
        for d in (generations, staging, migrations):
            d.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(wsp, "DISCOVERY_GENERATIONS_DIR", generations)
        monkeypatch.setattr(wsp, "STAGING_DIR", staging)
        monkeypatch.setattr(wsp, "DISCOVERY_MIGRATIONS_DIR", migrations)
        monkeypatch.setattr(wsp, "ACTIVE_GENERATION_PATH", tmp_path / "active_generation.json")
        monkeypatch.setattr(
            wsp, "DISCOVERY_MAINTENANCE_LOCK_PATH", migrations / ".maintenance.lock"
        )
        return SimpleNamespace(
            wsp=wsp,
            tmp_path=tmp_path,
            generations=generations,
            staging=staging,
            migrations=migrations,
            active_path=tmp_path / "active_generation.json",
        )

    @staticmethod
    def _make_staging(ns, gid: str, manifest_bytes: bytes = b'{"ok": true}'):
        ws = ns.wsp.create_staging_workspace(gid)
        (ws.root / "workspace.json").write_bytes(manifest_bytes)
        return ws

    def test_normal_commit_records_previous_pointer_and_snapshot(self, iso_ws):
        ns = iso_ws
        old_pointer = _pointer("gen-old", b"old", migration_id="mig-0")
        ns.active_path.write_text(
            json.dumps(old_pointer.to_dict()), encoding="utf-8"
        )

        ws = self._make_staging(ns, "gen-new")
        manifest_bytes = (ws.root / "workspace.json").read_bytes()
        ns.wsp.commit_workspace(ws, _pointer("gen-new", manifest_bytes))

        committed = ActiveGenerationPointerV4.from_dict_strict(
            json.loads(ns.active_path.read_text(encoding="utf-8"))
        )
        assert committed.generation_id == "gen-new"
        assert committed.previous_generation_id == "gen-old"
        assert not ws.root.exists()
        assert (ns.generations / "gen-new").is_dir()

        snapshot = json.loads(
            (ns.migrations / "mig-1.previous_pointer.json").read_text(encoding="utf-8")
        )
        assert snapshot["superseded_by"] == "gen-new"
        assert snapshot["previous_pointer"]["generation_id"] == "gen-old"

    def test_commit_without_existing_pointer_leaves_previous_none(self, iso_ws):
        ns = iso_ws
        ws = self._make_staging(ns, "gen-first")
        manifest_bytes = (ws.root / "workspace.json").read_bytes()
        ns.wsp.commit_workspace(ws, _pointer("gen-first", manifest_bytes))

        committed = ActiveGenerationPointerV4.from_dict_strict(
            json.loads(ns.active_path.read_text(encoding="utf-8"))
        )
        assert committed.previous_generation_id is None
        snapshot = json.loads(
            (ns.migrations / "mig-1.previous_pointer.json").read_text(encoding="utf-8")
        )
        assert snapshot["previous_pointer"] is None

    def test_crash_after_rename_pointer_not_written_self_heals(self, iso_ws):
        ns = iso_ws
        ws = self._make_staging(ns, "gen-crash1")
        manifest_bytes = (ws.root / "workspace.json").read_bytes()
        pointer = _pointer("gen-crash1", manifest_bytes)
        # Simulate a crash after rename, before the pointer write.
        import os
        os.rename(str(ws.root), str(ns.generations / "gen-crash1"))

        ns.wsp.commit_workspace(ws, pointer)
        committed = ActiveGenerationPointerV4.from_dict_strict(
            json.loads(ns.active_path.read_text(encoding="utf-8"))
        )
        assert committed.generation_id == "gen-crash1"
        assert committed.workspace_manifest_sha256 == pointer.workspace_manifest_sha256

    def test_crash_after_pointer_write_is_idempotent(self, iso_ws):
        ns = iso_ws
        ws = self._make_staging(ns, "gen-crash2")
        manifest_bytes = (ws.root / "workspace.json").read_bytes()
        pointer = _pointer("gen-crash2", manifest_bytes)
        ns.wsp.commit_workspace(ws, pointer)

        # Rerun (caller retrying after a post-pointer crash) succeeds and
        # changes nothing.
        again = ns.wsp.commit_workspace(ws, pointer)
        assert again.generation_id == "gen-crash2"
        committed = ActiveGenerationPointerV4.from_dict_strict(
            json.loads(ns.active_path.read_text(encoding="utf-8"))
        )
        assert committed == pointer

    def test_renamed_target_with_hash_mismatch_fails_closed(self, iso_ws):
        ns = iso_ws
        ws = self._make_staging(ns, "gen-crash3")
        import os
        os.rename(str(ws.root), str(ns.generations / "gen-crash3"))
        # Pointer expects different content than the promoted workspace.json.
        bad_pointer = _pointer("gen-crash3", b"different")
        with pytest.raises(ns.wsp.CommitReconciliationError):
            ns.wsp.commit_workspace(ws, bad_pointer)
        assert not ns.active_path.exists()

    def test_both_staging_and_target_missing_fails_closed(self, iso_ws):
        ns = iso_ws
        ws = ns.wsp.DiscoveryWorkspace(
            generation_id="gen-ghost",
            root=ns.staging / "gen-ghost",
            keyword_notebook_dir=ns.staging / "gen-ghost" / "keyword_notebooks",
            page_journals_dir=ns.staging / "gen-ghost" / "page_journals",
            exports_dir=ns.staging / "gen-ghost" / "exports",
            reports_dir=ns.staging / "gen-ghost" / "reports",
            locks_dir=ns.staging / "gen-ghost" / "locks",
        )
        with pytest.raises(ns.wsp.CommitReconciliationError):
            ns.wsp.commit_workspace(ws, _pointer("gen-ghost", b"{}"))

    def test_lock_contention_fails_fast(self, iso_ws):
        ns = iso_ws
        ws = self._make_staging(ns, "gen-locked")
        manifest_bytes = (ws.root / "workspace.json").read_bytes()
        lock = FileLock(str(ns.migrations / ".maintenance.lock"), timeout=0)
        with lock:
            with pytest.raises(ns.wsp.CommitLockError):
                ns.wsp.commit_workspace(ws, _pointer("gen-locked", manifest_bytes))
        # After the lock is released the commit succeeds.
        ns.wsp.commit_workspace(ws, _pointer("gen-locked", manifest_bytes))
        assert ns.active_path.exists()
