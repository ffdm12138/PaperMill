"""Integration tests: verify cache isolation with real subprocess invocations.

These tests spin up a real Python subprocess inside a TestRuntimeWorkspace
and verify that bytecode cache and temp files go to the right place.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from scripts.test_runtime_workspace import TestRuntimeWorkspace


pytestmark = pytest.mark.process


def _python() -> str:
    return sys.executable


# ---------------------------------------------------------------------------
# Cache isolation via PYTHONPYCACHEPREFIX
# ---------------------------------------------------------------------------

class TestCacheIsolation:
    def test_pycache_prefix_respected(self):
        """Bytecode is written to workspace cache_dir, not repo or C:\\."""
        with TestRuntimeWorkspace(group="isolation_cache") as ws:
            env = ws.child_env()
            # Run a python -c that imports a stdlib module to trigger .pyc creation
            # (use a module we know is NOT already cached in the workspace)
            script = (
                "import sys, os, tempfile; "
                "print('pycache_prefix:', sys.pycache_prefix, end=''); "
                "print('|tmpdir:', tempfile.gettempdir())"
            )
            result = subprocess.run(
                [_python(), "-c", script],
                env=env,
                capture_output=True,
                text=True,
                shell=False,
            )
            assert result.returncode == 0, result.stderr
            output = result.stdout.strip()
            assert str(ws.cache_dir) in output, (
                f"sys.pycache_prefix not set correctly: {output}"
            )

    def test_pyc_not_written_when_dont_write_bytecode(self):
        """With PYTHONDONTWRITEBYTECODE=1 in child_env, no .pyc is created
        anywhere — not in the workspace cache, not in the repo."""
        with TestRuntimeWorkspace(group="isolation_no_pyc") as ws:
            env = ws.child_env()
            with tempfile.TemporaryDirectory() as td:
                mod_dir = Path(td)
                (mod_dir / "temp_mod_nopyc_test.py").write_text(
                    "X = 42\n", encoding="utf-8"
                )
                script = (
                    f"import sys; sys.path.insert(0, {str(mod_dir)!r}); "
                    f"import temp_mod_nopyc_test; "
                    f"print('X:', temp_mod_nopyc_test.X)"
                )
                result = subprocess.run(
                    [_python(), "-c", script],
                    env=env,
                    capture_output=True,
                    text=True,
                    shell=False,
                )
                assert result.returncode == 0, result.stderr

            # No .pyc files should exist in workspace cache
            pyc_files = list(ws.cache_dir.rglob("*.pyc"))
            assert len(pyc_files) == 0, (
                f"PYTHONDONTWRITEBYTECODE=1 should prevent .pyc: {pyc_files}"
            )

    def test_pyc_not_in_repo_after_import(self):
        """Verify that importing a module does NOT create __pycache__ in repo."""
        repo_root = Path(__file__).resolve().parent.parent.parent
        pycache_before = set(repo_root.rglob("__pycache__"))

        with TestRuntimeWorkspace(group="isolation_norepo") as ws:
            env = ws.child_env()
            # Use a stdlib module import
            result = subprocess.run(
                [_python(), "-c", "import json, pathlib, hashlib"],
                env=env,
                capture_output=True,
                text=True,
                shell=False,
            )
            assert result.returncode == 0, result.stderr

        # No new __pycache__ in repo
        pycache_after = set(repo_root.rglob("__pycache__"))
        new_pycache = pycache_after - pycache_before
        # Filter out data/ dirs
        new_in_src = [
            p for p in new_pycache
            if not any(part in ("data", "output", "reports", "write") for part in p.parts)
        ]
        assert len(new_in_src) == 0, (
            f"New __pycache__ dirs created in repo: {new_in_src}"
        )


# ---------------------------------------------------------------------------
# TMP/TEMP isolation
# ---------------------------------------------------------------------------

class TestTempIsolation:
    def test_tempfile_uses_workspace(self):
        """tempfile.gettempdir() returns workspace temp_dir when TMP is set."""
        with TestRuntimeWorkspace(group="isolation_temp") as ws:
            env = ws.child_env()
            result = subprocess.run(
                [_python(), "-c",
                 "import tempfile; print(tempfile.gettempdir())"],
                env=env,
                capture_output=True,
                text=True,
                shell=False,
            )
            assert result.returncode == 0, result.stderr
            output = result.stdout.strip()
            assert str(ws.temp_dir) in output, (
                f"gettempdir() returned {output}, expected {ws.temp_dir}"
            )

    def test_tempfile_creates_in_workspace(self):
        """TemporaryDirectory() creates dirs inside workspace temp."""
        with TestRuntimeWorkspace(group="isolation_tmpdir") as ws:
            env = ws.child_env()
            script = (
                "import tempfile, pathlib; "
                "td = tempfile.TemporaryDirectory(); "
                "p = pathlib.Path(td.name); "
                "print(p); "
                "td.cleanup()"
            )
            result = subprocess.run(
                [_python(), "-c", script],
                env=env,
                capture_output=True,
                text=True,
                shell=False,
            )
            assert result.returncode == 0, result.stderr
            output = result.stdout.strip()
            assert str(ws.temp_dir) in output, (
                f"TemporaryDirectory created outside workspace: {output}"
            )


# ---------------------------------------------------------------------------
# Path preservation in env
# ---------------------------------------------------------------------------

class TestPathPreservationInSubprocess:
    def test_path_not_flattened(self):
        """Env paths received by subprocess are not flattened."""
        with TestRuntimeWorkspace(group="isolation_path") as ws:
            env = ws.child_env()
            script = (
                "import os; "
                "prefix = os.environ['PYTHONPYCACHEPREFIX']; "
                "tmp = os.environ['TMP']; "
                "print('PYCACHE:', prefix); "
                "print('TMP:', tmp); "
            )
            result = subprocess.run(
                [_python(), "-c", script],
                env=env,
                capture_output=True,
                text=True,
                shell=False,
            )
            assert result.returncode == 0, result.stderr
            output = result.stdout
            if os.name == "nt":
                # Check that paths contain drive colon (not flattened)
                assert ":\\" in output or ":/" in output, (
                    f"Paths lack drive colon — may be flattened:\n{output}"
                )
                # Check that paths DON'T contain the flattened pattern
                # (e.g., "UsersAdmin" without backslash before it)
                lines = output.strip().splitlines()
                for line in lines:
                    if "PYCACHE:" in line:
                        path_part = line.split("PYCACHE:", 1)[1].strip()
                        # Should NOT look like "C:UsersAdmin..." (colon followed by letter, no backslash)
                        import re
                        assert not re.search(r"[A-Z]:[A-Z]", path_part), (
                            f"Path appears flattened: {path_part!r}"
                        )
            else:
                # POSIX: workspace paths must pass through unchanged
                assert f"PYCACHE: {ws.cache_dir}" in output, (
                    f"PYTHONPYCACHEPREFIX altered in subprocess:\n{output}"
                )
                assert f"TMP: {ws.temp_dir}" in output, (
                    f"TMP altered in subprocess:\n{output}"
                )

