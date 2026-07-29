"""Unit tests for TestRuntimeWorkspace — path preservation, env correctness, lifecycle."""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from scripts.test_runtime_workspace import (
    TestRuntimeWorkspace,
    _flatten_path,
    _pid_alive,
    _windows_readonly_retry_handler,
    count_repo_pyc,
    count_repo_pycache,
    count_root_pollution,
    is_stale_workspace,
    remove_verified_workspace_tree,
)


# ---------------------------------------------------------------------------
# _flatten_path
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name != "nt", reason="_flatten_path requires a native Windows Path")
class TestFlattenPath:
    def test_windows_temp_path(self):
        result = _flatten_path(Path(r"C:\Users\Admin\AppData\Local\Temp"))
        assert result == "UsersAdminAppDataLocalTemp"
        assert ":" not in result
        assert "\\" not in result

    def test_no_trailing_separator(self):
        result = _flatten_path(Path(r"C:\Users\Admin\AppData\Local\Temp"))
        assert not result.startswith("C")

    def test_preserves_non_separator_chars(self):
        result = _flatten_path(Path(r"C:\foo_bar-baz\qux"))
        assert "foo_bar-baz" in result


# ---------------------------------------------------------------------------
# Workspace lifecycle
# ---------------------------------------------------------------------------

class TestWorkspaceLifecycle:
    def test_create_and_cleanup(self):
        """Workspace is created on enter and removed on exit."""
        with TestRuntimeWorkspace(group="lifecycle_test") as ws:
            assert ws.root.exists()
            assert ws.cache_dir.exists()
            assert ws.pytest_dir.exists()
            assert ws.temp_dir.exists()
            assert ws.home_dir.exists()
            assert ws.logs_dir.exists()
            marker = ws.root / ".mineru-test-workspace.json"
            assert marker.is_file()
        # After exit the workspace should be gone
        # (root raises RuntimeError after cleanup, which is correct)

    def test_cleanup_on_exception(self):
        """Workspace is cleaned up even when an exception occurs inside."""
        ws_ref: list[Path] = []
        try:
            with TestRuntimeWorkspace(group="exception_test") as ws:
                ws_ref.append(ws.root)
                raise ValueError("simulated failure")
        except ValueError:
            pass
        assert len(ws_ref) == 1
        assert not ws_ref[0].exists()

    def test_repeat_workspaces_no_collision(self):
        """Repeated workspace creation does not leak or collide."""
        roots = []
        for i in range(3):
            with TestRuntimeWorkspace(group="repeat_test") as ws:
                roots.append(ws.root)
                assert ws.root.exists()
        assert len(roots) == 3
        assert len(set(str(r) for r in roots)) == 3
        for r in roots:
            assert not r.exists()

    def test_marker_content(self):
        """Marker contains correct metadata."""
        with TestRuntimeWorkspace(group="marker_test") as ws:
            marker = ws.root / ".mineru-test-workspace.json"
            data = json.loads(marker.read_text(encoding="utf-8"))
            assert data["schema_version"] == "1.0"
            assert data["owner"] == "mineru-test-runner"
            assert data["group"] == "marker_test"
            assert data["pid"] == os.getpid()
            assert "repo_root" in data
            assert "created_at" in data

    def test_partial_cleanup_failure_restores_complete_marker(self, monkeypatch):
        """A marker deleted by partial rmtree is restored with its identity."""
        ws = TestRuntimeWorkspace(group="marker_restore")
        ws.__enter__()
        root = ws.root
        marker_path = root / ".mineru-test-workspace.json"
        original_marker = json.loads(marker_path.read_text(encoding="utf-8"))
        real_rmtree = shutil.rmtree

        def _partial_failure(path, *args, **kwargs):
            candidate_marker = Path(path) / ".mineru-test-workspace.json"
            if candidate_marker.exists():
                candidate_marker.unlink()
            raise PermissionError("simulated Windows read-only file")

        monkeypatch.setattr("scripts.test_runtime_workspace.shutil.rmtree", _partial_failure)
        monkeypatch.setattr("scripts.test_runtime_workspace.time.sleep", lambda _delay: None)
        try:
            result = ws.cleanup()
            assert not result.success
            assert result.attempts == 5
            restored = json.loads(marker_path.read_text(encoding="utf-8"))
            for key in (
                "schema_version", "owner", "group", "pid", "created_at", "repo_root",
            ):
                assert restored[key] == original_marker[key]
            assert restored["cleanup_status"] == "failed"
            assert restored["cleanup_attempts"] == 5
            assert "read-only" in restored["cleanup_last_error"]
        finally:
            real_rmtree(root, ignore_errors=True)

    def test_marker_identity_change_blocks_first_mutation(self, monkeypatch):
        ws = TestRuntimeWorkspace(group="identity_change")
        ws.__enter__()
        root = ws.root
        marker_path = root / ".mineru-test-workspace.json"
        expected = json.loads(marker_path.read_text(encoding="utf-8"))
        changed = dict(expected)
        changed["owner"] = "not-mineru"
        marker_path.write_text(json.dumps(changed), encoding="utf-8")
        calls: list[Path] = []
        real_rmtree = shutil.rmtree
        monkeypatch.setattr(
            "scripts.test_runtime_workspace.shutil.rmtree",
            lambda path, **_kwargs: calls.append(Path(path)),
        )
        try:
            result = remove_verified_workspace_tree(
                root,
                marker_snapshot=expected,
                repo_root=Path(expected["repo_root"]),
            )
            assert not result.success
            assert result.attempts == 0
            assert "identity changed" in str(result.error)
            assert calls == []
            assert root.exists()
        finally:
            real_rmtree(root, ignore_errors=True)

    @pytest.mark.skipif(os.name != "nt", reason="Windows read-only semantics")
    def test_windows_readonly_file_is_cleaned_inside_verified_workspace(self):
        root: Path
        with TestRuntimeWorkspace(group="readonly_cleanup") as ws:
            root = ws.root
            readonly = root / "readonly.txt"
            readonly.write_text("locked", encoding="utf-8")
            os.chmod(readonly, stat.S_IREAD)
        assert not root.exists()

    def test_readonly_handler_never_chmods_outside_workspace(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        chmod_calls: list[Path] = []
        monkeypatch.setattr(os, "chmod", lambda path, _mode: chmod_calls.append(Path(path)))
        handler = _windows_readonly_retry_handler(root)
        error = PermissionError("denied")
        with pytest.raises(PermissionError):
            handler(os.unlink, str(outside), (PermissionError, error, None))
        assert chmod_calls == []
        assert outside.exists()


# ---------------------------------------------------------------------------
# child_env correctness
# ---------------------------------------------------------------------------

class TestChildEnv:
    def test_cache_prefix_set(self):
        with TestRuntimeWorkspace(group="env_test") as ws:
            env = ws.child_env()
            assert env["PYTHONPYCACHEPREFIX"] == str(ws.cache_dir)

    def test_temp_vars_set(self):
        with TestRuntimeWorkspace(group="env_test") as ws:
            env = ws.child_env()
            assert env["TMP"] == str(ws.temp_dir)
            assert env["TEMP"] == str(ws.temp_dir)
            assert env["TMPDIR"] == str(ws.temp_dir)

    def test_pytest_disable_plugin_autoload(self):
        with TestRuntimeWorkspace(group="env_test") as ws:
            env = ws.child_env()
            assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"

    @pytest.mark.skipif(os.name != "nt", reason="Windows path semantics")
    def test_windows_backslashes_preserved(self):
        """Paths in child_env must use native Windows backslashes."""
        with TestRuntimeWorkspace(group="env_test") as ws:
            env = ws.child_env()
            prefix = env["PYTHONPYCACHEPREFIX"]
            # Must contain backslash path separators on Windows
            assert "\\" in prefix, f"Backslashes stripped from {prefix!r}"
            # Must NOT be flattened (no colons removed)
            assert ":" in prefix, f"Drive colon stripped from {prefix!r}"
            # Must end with correct subdir
            assert prefix.endswith("\\cache") or prefix.endswith("/cache")

    def test_extra_env_merged(self):
        with TestRuntimeWorkspace(group="env_test") as ws:
            env = ws.child_env(extra={"MY_VAR": "my_value"})
            assert env["MY_VAR"] == "my_value"
            assert env["PYTHONPYCACHEPREFIX"] == str(ws.cache_dir)

    def test_home_not_set_by_default(self):
        with TestRuntimeWorkspace(group="env_test") as ws:
            env = ws.child_env()
            # HOME should NOT be overridden unless set_home=True
            assert env.get("HOME") == os.environ.get("HOME")

    def test_home_set_when_requested(self):
        with TestRuntimeWorkspace(group="env_test", set_home=True) as ws:
            env = ws.child_env()
            assert env["HOME"] == str(ws.home_dir)
            if os.name == "nt":
                assert env["USERPROFILE"] == str(ws.home_dir)


# ---------------------------------------------------------------------------
# is_stale_workspace
# ---------------------------------------------------------------------------

class TestIsStaleWorkspace:
    def test_active_workspace_not_stale(self):
        with TestRuntimeWorkspace(group="stale_test") as ws:
            reason = is_stale_workspace(ws.root)
            # Owner PID is alive, so should NOT be stale
            assert reason is not None  # has a reason for why NOT stale
            assert "alive" in reason

    def test_no_marker_not_stale(self, tmp_path):
        """Directory without a marker is not a recognised workspace.

        is_stale_workspace returns a reason string for non-STALE status,
        None only for STALE (safe to delete)."""
        d = tmp_path / "mineru_something"
        d.mkdir()
        reason = is_stale_workspace(d)
        # UNRECOGNIZED → reason string (not None), NOT safe to delete
        assert reason is not None and "recognised" in reason

    def test_wrong_owner(self, tmp_path):
        d = tmp_path / "mineru_test"
        d.mkdir()
        marker = d / ".mineru-test-workspace.json"
        marker.write_text(json.dumps({
            "schema_version": "1.0",
            "owner": "some-other-tool",
            "pid": 99999,
            "repo_root": str(Path.cwd()),
            "group": "test",
            "created_at": "2026-01-01T00:00:00+00:00",
        }))
        reason = is_stale_workspace(d)
        # Wrong owner → NOT stale (don't touch it).  is_stale_workspace
        # returns None for "safe to delete" and a reason string otherwise.
        assert reason is not None, "Wrong-owner workspace should NOT be marked stale"

    def test_dead_pid_is_stale(self, tmp_path):
        d = tmp_path / "mineru_test"
        d.mkdir()
        marker = d / ".mineru-test-workspace.json"
        marker.write_text(json.dumps({
            "schema_version": "1.0",
            "owner": "mineru-test-runner",
            "pid": 99999999,
            "repo_root": str(Path.cwd().resolve()),
            "group": "test",
            "created_at": "2026-01-01T00:00:00+00:00",
        }))
        reason = is_stale_workspace(d)
        assert reason is None  # None = safe to delete (stale)


# ---------------------------------------------------------------------------
# Pollution counters
# ---------------------------------------------------------------------------

class TestPollutionCounters:
    pass
