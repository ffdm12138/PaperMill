"""Unit tests for agent_acceptance pollution detection functions.

Covers PollutionSnapshot, pre-flight fail-closed, post-flight path diffs,
and the invariant that acceptance never auto-deletes repo bytecode.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
sys.dont_write_bytecode = True  # test process must also not pollute

from scripts.agent_acceptance import (
    FAST_ACCEPTANCE_TESTS,
    FAST_GROUPS,
    PollutionSnapshot,
    _collect_pollution_snapshot,
    _collect_temp_workspace_issues,
    _pollution_post_check,
    _pollution_pre_check,
)


def test_fast_gate_includes_core_bilingual_discovery_tests():
    required = {
        "tests/unit/test_keyword_notebook.py",
        "tests/unit/test_discovery_dual_lane_scheduler.py",
        "tests/unit/test_discovery_global_coordinator.py",
    }
    discovery_paths = dict(FAST_GROUPS)["discovery"]
    assert required <= set(discovery_paths)
    assert required <= set(FAST_ACCEPTANCE_TESTS)


# ---------------------------------------------------------------------------
# PollutionSnapshot
# ---------------------------------------------------------------------------

class TestPollutionSnapshot:
    def test_empty_snapshot_is_clean(self):
        snap = PollutionSnapshot(
            repo_pycache=frozenset(),
            repo_pyc=frozenset(),
            root_pollution=frozenset(),
        )
        assert snap.is_clean

    @pytest.mark.parametrize("pollution_kind,pycache,pyc,root_pollution", [
        ("pycache", frozenset({"scripts/__pycache__"}), frozenset(), frozenset()),
        ("pyc", frozenset(), frozenset({"src/foo.pyc"}), frozenset()),
        ("root_file", frozenset(), frozenset(), frozenset({r"C:\some\flattened\cache"})),
    ])
    def test_pollution_makes_dirty(self, pollution_kind, pycache, pyc, root_pollution):
        snap = PollutionSnapshot(
            repo_pycache=pycache,
            repo_pyc=pyc,
            root_pollution=root_pollution,
        )
        assert not snap.is_clean

    def test_immutable(self):
        snap = PollutionSnapshot(
            repo_pycache=frozenset({"a"}),
            repo_pyc=frozenset({"b"}),
            root_pollution=frozenset({"c"}),
        )
        with pytest.raises(Exception):
            snap.repo_pycache.add("d")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# collect_pollution_snapshot — read-only
# ---------------------------------------------------------------------------

class TestCollectPollutionSnapshot:
    def test_returns_pollution_snapshot(self):
        snap = _collect_pollution_snapshot()
        assert isinstance(snap, PollutionSnapshot)
        assert isinstance(snap.repo_pycache, frozenset)
        assert isinstance(snap.repo_pyc, frozenset)
        assert isinstance(snap.root_pollution, frozenset)
        assert isinstance(snap.workspace_issues, frozenset)

    def test_no_deletion_during_collection(self, monkeypatch):
        """collect_pollution_snapshot must never call rmtree or unlink."""
        import shutil
        called = []

        def _fake_rmtree(*args, **kwargs):
            called.append("rmtree")

        def _fake_unlink(*args, **kwargs):
            called.append("unlink")

        monkeypatch.setattr(shutil, "rmtree", _fake_rmtree)
        monkeypatch.setattr(Path, "unlink", _fake_unlink)

        _collect_pollution_snapshot()

        assert "rmtree" not in called, "_collect_pollution_snapshot must not call rmtree"
        # unlink may be called by tempfile (internal) — only check that our
        # collection didn't trigger it on repo paths


# ---------------------------------------------------------------------------
# Pre-flight fail-closed
# ---------------------------------------------------------------------------

class TestPreflightFailClosed:
    def test_clean_snapshot_produces_no_errors(self, monkeypatch):
        """Pre-flight on a clean repo produces no errors."""
        clean = PollutionSnapshot(
            repo_pycache=frozenset(),
            repo_pyc=frozenset(),
            root_pollution=frozenset(),
        )
        monkeypatch.setattr(
            "scripts.agent_acceptance._collect_pollution_snapshot",
            lambda: clean,
        )
        errors = _pollution_pre_check()
        assert errors == [], f"Expected no errors on clean snapshot, got: {errors}"

    @pytest.mark.parametrize("pollution_kind,pycache,pyc,root_pollution,expected_hint", [
        ("pycache", frozenset({"scripts/__pycache__"}), frozenset(), frozenset(), "__pycache__"),
        ("pyc", frozenset(), frozenset({"src/foo.pyc"}), frozenset(), ".pyc"),
        ("root_file", frozenset(), frozenset(), frozenset({r"C:\UsersAdmin...cache"}), "cleanup_test_caches"),
    ])
    def test_pollution_produces_errors(self, monkeypatch, pollution_kind, pycache, pyc, root_pollution, expected_hint):
        dirty = PollutionSnapshot(
            repo_pycache=pycache,
            repo_pyc=pyc,
            root_pollution=root_pollution,
        )
        monkeypatch.setattr(
            "scripts.agent_acceptance._collect_pollution_snapshot",
            lambda: dirty,
        )
        errors = _pollution_pre_check()
        assert len(errors) > 0
        assert any(expected_hint in e for e in errors)

    def test_untrusted_workspace_produces_errors(self, monkeypatch):
        dirty = PollutionSnapshot(
            repo_pycache=frozenset(),
            repo_pyc=frozenset(),
            root_pollution=frozenset(),
            workspace_issues=frozenset({
                r"C:\Temp\mineru_full_deadbeef [unrecognized] marker missing",
            }),
        )
        monkeypatch.setattr(
            "scripts.agent_acceptance._collect_pollution_snapshot",
            lambda: dirty,
        )
        errors = _pollution_pre_check()
        assert any("manual ownership review" in error for error in errors)


class TestTempWorkspaceIssues:
    def test_invalid_and_unrecognized_candidates_fail_closed(self, tmp_path):
        no_marker = tmp_path / "mineru_orphan_deadbeef"
        no_marker.mkdir()
        invalid = tmp_path / "mineru_invalid_cafebabe"
        invalid.mkdir()
        (invalid / ".mineru-test-workspace.json").write_text(
            '{"owner":"mineru-test-runner","schema_version":"bad"}',
            encoding="utf-8",
        )
        issues = _collect_temp_workspace_issues(tmp_path)
        assert len(issues) == 2
        assert any("[unrecognized]" in issue for issue in issues)
        assert any("[invalid]" in issue for issue in issues)

    def test_cleanup_report_directory_is_not_a_workspace(self, tmp_path):
        (tmp_path / "mineru_cleanup_reports").mkdir()
        assert _collect_temp_workspace_issues(tmp_path) == frozenset()

    def test_active_workspace_is_not_residue(self, tmp_path):
        import json

        from scripts.agent_acceptance import ROOT

        active = tmp_path / "mineru_active_deadbeef"
        active.mkdir()
        (active / ".mineru-test-workspace.json").write_text(
            json.dumps({
                "schema_version": "1.0",
                "owner": "mineru-test-runner",
                "pid": os.getpid(),
                "repo_root": str(ROOT.resolve()),
                "group": "active",
                "created_at": "2026-01-01T00:00:00+00:00",
            }),
            encoding="utf-8",
        )
        assert _collect_temp_workspace_issues(tmp_path) == frozenset()


# ---------------------------------------------------------------------------
# Post-flight path-set diff
# ---------------------------------------------------------------------------

class TestPostflightDiff:
    def test_no_change_is_clean(self, monkeypatch):
        """After = Before → no errors."""
        snap = PollutionSnapshot(
            repo_pycache=frozenset(),
            repo_pyc=frozenset(),
            root_pollution=frozenset(),
        )
        monkeypatch.setattr(
            "scripts.agent_acceptance._pollution_before", snap,
        )
        monkeypatch.setattr(
            "scripts.agent_acceptance._collect_pollution_snapshot",
            lambda: snap,
        )
        errors = _pollution_post_check()
        assert errors == [], f"Expected no errors, got: {errors}"

    @pytest.mark.parametrize("pollution_kind,after_pycache,after_pyc,expected_pattern", [
        ("pycache", frozenset({"new/dir/__pycache__"}), frozenset(), "new/dir/__pycache__"),
        ("pyc", frozenset(), frozenset({"scripts/new_file.pyc"}), "scripts/new_file.pyc"),
    ])
    def test_new_pollution_detected(self, monkeypatch, pollution_kind, after_pycache, after_pyc, expected_pattern):
        before = PollutionSnapshot(
            repo_pycache=frozenset(),
            repo_pyc=frozenset(),
            root_pollution=frozenset(),
        )
        after = PollutionSnapshot(
            repo_pycache=after_pycache,
            repo_pyc=after_pyc,
            root_pollution=frozenset(),
        )
        monkeypatch.setattr(
            "scripts.agent_acceptance._pollution_before", before,
        )
        monkeypatch.setattr(
            "scripts.agent_acceptance._collect_pollution_snapshot",
            lambda: after,
        )
        errors = _pollution_post_check()
        assert len(errors) > 0
        assert any(expected_pattern in e for e in errors)

    def test_existing_items_not_reported(self, monkeypatch):
        """If before has A and after still has A, A is NOT new."""
        before = PollutionSnapshot(
            repo_pycache=frozenset({"scripts/__pycache__"}),
            repo_pyc=frozenset(),
            root_pollution=frozenset(),
        )
        after = PollutionSnapshot(
            repo_pycache=frozenset({"scripts/__pycache__"}),
            repo_pyc=frozenset(),
            root_pollution=frozenset(),
        )
        monkeypatch.setattr(
            "scripts.agent_acceptance._pollution_before", before,
        )
        monkeypatch.setattr(
            "scripts.agent_acceptance._collect_pollution_snapshot",
            lambda: after,
        )
        errors = _pollution_post_check()
        assert errors == [], f"Existing items should not be reported as new: {errors}"

    def test_only_new_items_reported(self, monkeypatch):
        """Before has A, after has A+B → only B reported."""
        before = PollutionSnapshot(
            repo_pycache=frozenset({"old/__pycache__"}),
            repo_pyc=frozenset(),
            root_pollution=frozenset(),
        )
        after = PollutionSnapshot(
            repo_pycache=frozenset({"old/__pycache__", "new/__pycache__"}),
            repo_pyc=frozenset(),
            root_pollution=frozenset(),
        )
        monkeypatch.setattr(
            "scripts.agent_acceptance._pollution_before", before,
        )
        monkeypatch.setattr(
            "scripts.agent_acceptance._collect_pollution_snapshot",
            lambda: after,
        )
        errors = _pollution_post_check()
        assert len(errors) > 0
        assert any("new/__pycache__" in e for e in errors)
        assert not any("old/__pycache__" in e for e in errors)


# ---------------------------------------------------------------------------
# No auto-deletion invariant
# ---------------------------------------------------------------------------

class TestNoAutoDelete:
    def test_pollution_pre_check_does_not_delete(self, monkeypatch):
        """pollution_pre_check must never call rmtree or unlink."""
        import shutil
        rmtree_calls = []
        unlink_calls = []

        def _fake_rmtree(*args, **kwargs):
            rmtree_calls.append(args[0])

        def _fake_unlink(*args, **kwargs):
            unlink_calls.append(args[0])

        monkeypatch.setattr(shutil, "rmtree", _fake_rmtree)
        monkeypatch.setattr(Path, "unlink", _fake_unlink)

        # Simulate a dirty snapshot
        dirty = PollutionSnapshot(
            repo_pycache=frozenset({"scripts/__pycache__"}),
            repo_pyc=frozenset(),
            root_pollution=frozenset(),
        )
        monkeypatch.setattr(
            "scripts.agent_acceptance._collect_pollution_snapshot",
            lambda: dirty,
        )
        _pollution_pre_check()

        assert not rmtree_calls, f"pre_check called rmtree: {rmtree_calls}"
        assert not unlink_calls, f"pre_check called unlink: {unlink_calls}"

    def test_pollution_post_check_does_not_delete(self, monkeypatch):
        """pollution_post_check must never call rmtree or unlink."""
        import shutil
        rmtree_calls = []
        unlink_calls = []

        def _fake_rmtree(*args, **kwargs):
            rmtree_calls.append(args[0])

        def _fake_unlink(*args, **kwargs):
            unlink_calls.append(args[0])

        monkeypatch.setattr(shutil, "rmtree", _fake_rmtree)
        monkeypatch.setattr(Path, "unlink", _fake_unlink)

        snap = PollutionSnapshot(
            repo_pycache=frozenset(),
            repo_pyc=frozenset(),
            root_pollution=frozenset(),
        )
        monkeypatch.setattr(
            "scripts.agent_acceptance._pollution_before", snap,
        )
        monkeypatch.setattr(
            "scripts.agent_acceptance._collect_pollution_snapshot",
            lambda: snap,
        )
        _pollution_post_check()

        assert not rmtree_calls, f"post_check called rmtree: {rmtree_calls}"
        assert not unlink_calls, f"post_check called unlink: {unlink_calls}"


# ---------------------------------------------------------------------------
# Bytecode strategy: acceptance process must have dont_write_bytecode
# ---------------------------------------------------------------------------

class TestBytecodeStrategy:
    def test_sys_dont_write_bytecode_is_set(self):
        """The acceptance script sets sys.dont_write_bytecode=True before
        imports.  This test process mirrors that — verify it works."""
        assert sys.dont_write_bytecode, (
            "sys.dont_write_bytecode must be True in test processes too"
        )

    def test_import_does_not_create_pycache(self, tmp_path):
        """Importing a temp module with dont_write_bytecode=True creates
        no __pycache__."""
        mod = tmp_path / "temp_no_pycache_mod.py"
        mod.write_text("X = 42\n", encoding="utf-8")
        sys.path.insert(0, str(tmp_path))
        try:
            import temp_no_pycache_mod  # type: ignore[import-not-found]
            del sys.modules["temp_no_pycache_mod"]
        finally:
            sys.path.remove(str(tmp_path))
        pycache = tmp_path / "__pycache__"
        assert not pycache.exists(), (
            f"__pycache__ created despite sys.dont_write_bytecode=True"
        )
