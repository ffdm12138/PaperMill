"""Fresh-install bootstrap and explicit workspace resolution (v4).

``bootstrap_initial_workspace`` is the sole production entry point that
creates and activates the first v4 generation; ``resolve_explicit_workspace``
is the sole entry point for explicit test/staging roots.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.discovery.workspace as wsp
from src.discovery.contracts.manifest import ActiveGenerationPointerV4
from tests.helpers.discovery_workspace import make_test_workspace


pytestmark = pytest.mark.unit


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """Isolate every discovery workspace path under tmp_path."""
    generations = tmp_path / "generations"
    staging = generations / ".staging"
    migrations = tmp_path / "migrations"
    for d in (generations, staging, migrations):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(wsp, "DISCOVERY_GENERATIONS_DIR", generations)
    monkeypatch.setattr(wsp, "STAGING_DIR", staging)
    monkeypatch.setattr(wsp, "DISCOVERY_MIGRATIONS_DIR", migrations)
    monkeypatch.setattr(
        wsp, "ACTIVE_GENERATION_PATH", tmp_path / "active_generation.json"
    )
    monkeypatch.setattr(
        wsp, "DISCOVERY_MAINTENANCE_LOCK_PATH", migrations / ".maintenance.lock"
    )
    return SimpleNamespace(
        tmp_path=tmp_path,
        generations=generations,
        active_path=tmp_path / "active_generation.json",
    )


class TestBootstrapInitialWorkspace:
    def test_fresh_bootstrap_creates_resolvable_active_generation(self, iso):
        ws, created = wsp.bootstrap_initial_workspace()

        assert created is True
        resolved = wsp.WorkspaceResolver().resolve_active()
        assert resolved.generation_id == ws.generation_id
        # Completely empty generation: no keyword notebooks enabled.
        assert list(resolved.keyword_notebook_dir.iterdir()) == []
        # The active pointer binds the exact workspace.json bytes.
        pointer = ActiveGenerationPointerV4.from_dict_strict(
            json.loads(iso.active_path.read_text(encoding="utf-8"))
        )
        assert pointer.migration_id == wsp.BOOTSTRAP_MIGRATION_ID
        assert pointer.previous_generation_id is None
        # Activation-time tree closure also verifies.
        wsp.WorkspaceResolver().resolve_active(verify_tree=True)

    def test_bootstrap_is_idempotent(self, iso):
        first, created_first = wsp.bootstrap_initial_workspace()
        second, created_second = wsp.bootstrap_initial_workspace()

        assert created_first is True
        assert created_second is False
        assert second.generation_id == first.generation_id
        # No leftover staging directory from either run.
        assert list((iso.generations / ".staging").iterdir()) == []

    def test_bootstrap_honors_explicit_generation_id(self, iso):
        ws, created = wsp.bootstrap_initial_workspace(generation_id="v4-fresh")

        assert created is True
        assert ws.generation_id == "v4-fresh"
        assert (iso.generations / "v4-fresh" / "workspace.json").is_file()

    def test_bootstrap_fails_closed_on_corrupt_pointer(self, iso):
        iso.active_path.write_text('{"corrupt": true}', encoding="utf-8")

        with pytest.raises(wsp.ActiveGenerationCorruptError):
            wsp.bootstrap_initial_workspace()
        # The corrupt pointer is never stomped.
        assert json.loads(iso.active_path.read_text(encoding="utf-8")) == {
            "corrupt": True
        }


class TestResolveExplicitWorkspace:
    def test_accepts_helper_built_workspace(self, tmp_path):
        made = make_test_workspace(tmp_path / "ws-ok")

        resolved = wsp.WorkspaceResolver.resolve_explicit_workspace(
            tmp_path / "ws-ok"
        )
        assert resolved.generation_id == "ws-ok"
        assert resolved.keyword_notebook_dir.is_dir()

    def test_rejects_missing_root(self, tmp_path):
        with pytest.raises(wsp.WorkspaceIncompleteError):
            wsp.WorkspaceResolver.resolve_explicit_workspace(tmp_path / "nope")

    def test_rejects_missing_manifest(self, tmp_path):
        root = tmp_path / "ws-nomanifest"
        ws = make_test_workspace(root)
        (ws.root / "workspace.json").unlink()

        with pytest.raises(wsp.WorkspaceManifestMissingError):
            wsp.WorkspaceResolver.resolve_explicit_workspace(root)

    def test_rejects_generation_id_mismatch(self, tmp_path):
        root = tmp_path / "ws-renamed"
        ws = make_test_workspace(root)
        manifest = wsp.build_workspace_manifest(
            "other-generation", ws.root, migration_id="test-bootstrap"
        )
        wsp.write_workspace_manifest(ws.root, manifest)

        with pytest.raises(
            wsp.WorkspaceManifestMismatchError, match="generation_id"
        ):
            wsp.WorkspaceResolver.resolve_explicit_workspace(root)

    def test_rejects_missing_subdirectory(self, tmp_path):
        root = tmp_path / "ws-incomplete"
        ws = make_test_workspace(root)
        import shutil

        shutil.rmtree(ws.locks_dir)

        with pytest.raises(wsp.WorkspaceIncompleteError, match="locks"):
            wsp.WorkspaceResolver.resolve_explicit_workspace(root)

    def test_verify_tree_detects_runtime_drift(self, tmp_path):
        root = tmp_path / "ws-drift"
        ws = make_test_workspace(root)
        (ws.keyword_notebook_dir / "nb.json").write_text("{}", encoding="utf-8")

        # Default resolution tolerates runtime content drift...
        wsp.WorkspaceResolver.resolve_explicit_workspace(root)
        # ...while the opt-in tree check reports it.
        with pytest.raises(
            wsp.WorkspaceManifestMismatchError, match="tree SHA-256"
        ):
            wsp.WorkspaceResolver.resolve_explicit_workspace(
                root, verify_tree=True
            )

    def test_runtime_context_uses_strict_explicit_resolution(self, tmp_path):
        from src.discovery.runtime_context import (
            DiscoveryRuntimeUnavailableError,
            resolve_active_runtime,
        )

        # A bare directory with only keyword_notebooks/ no longer resolves.
        bare = tmp_path / "legacy-flat"
        (bare / "keyword_notebooks").mkdir(parents=True)
        with pytest.raises(DiscoveryRuntimeUnavailableError):
            resolve_active_runtime(workspace_root=bare)

        # A complete helper-built workspace resolves.
        made = make_test_workspace(tmp_path / "ws-ctx")
        ctx = resolve_active_runtime(workspace_root=made.root)
        assert ctx.notebook_root == made.root / "keyword_notebooks"


class TestInitDiscoveryWorkspaceCLI:
    def test_fresh_install_then_idempotent(self, iso, capsys):
        import scripts.init_discovery_workspace as init_cli

        assert init_cli.main([]) == 0
        out = capsys.readouterr().out
        assert "[OK] initialized discovery v4 workspace" in out

        assert init_cli.main([]) == 0
        out = capsys.readouterr().out
        assert "already present" in out

        # The resulting generation resolves through the production path.
        resolved = wsp.WorkspaceResolver().resolve_active()
        assert list(resolved.keyword_notebook_dir.iterdir()) == []

    def test_corrupt_pointer_fails_closed(self, iso, capsys):
        import scripts.init_discovery_workspace as init_cli

        iso.active_path.write_text('{"corrupt": true}', encoding="utf-8")
        assert init_cli.main([]) == 1
        assert "[ERROR]" in capsys.readouterr().err
