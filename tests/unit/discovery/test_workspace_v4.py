"""Unit tests for v4 DiscoveryWorkspace."""
from __future__ import annotations

import pytest
import shutil
import tempfile
from pathlib import Path

from src.discovery.workspace import (
    DiscoveryWorkspace,
    WorkspaceResolver,
    create_staging_workspace,
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
            "keyword_notebook_dir", "lane_states_dir", "page_journals_dir",
            "pending_candidates_dir", "indexes_dir", "exports_dir",
            "reports_dir", "locks_dir",
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
                lane_states_dir=root / "lane_states",
                page_journals_dir=root / "page_journals",
                pending_candidates_dir=root / "pending_candidates",
                indexes_dir=root / "indexes",
                exports_dir=root / "exports",
                reports_dir=root / "reports",
                locks_dir=root / "locks",
            )
            ws.ensure_dirs()
            dir_attrs = [
                ws.keyword_notebook_dir, ws.lane_states_dir,
                ws.page_journals_dir, ws.pending_candidates_dir,
                ws.indexes_dir, ws.exports_dir,
                ws.reports_dir, ws.locks_dir,
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
                lane_states_dir=root / "lane_states",
                page_journals_dir=root / "page_journals",
                pending_candidates_dir=root / "pending_candidates",
                indexes_dir=root / "indexes",
                exports_dir=root / "exports",
                reports_dir=root / "reports",
                locks_dir=root / "locks",
            )
            ws.ensure_dirs()
            ws.ensure_dirs()  # second call should not raise
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
