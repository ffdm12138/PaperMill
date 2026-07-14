"""Unit tests for cleanup_test_caches.py — safety gates, content checks, dry-run."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from scripts.cleanup_test_caches import (
    _check_cache_only_content,
    _delete_entry,
    _dir_size_and_count,
    _is_safe_to_delete,
    _scan_legacy_flattened,
    _scan_stale_workspaces,
)
from scripts.test_runtime_workspace import _system_temp_dir


# ---------------------------------------------------------------------------
# _check_cache_only_content
# ---------------------------------------------------------------------------

class TestCacheOnlyContent:
    # Standard pytest cache directory signature
    _CACHEDIR_TAG = (
        "Signature: 8a477f597d28d172789f06886806bc55\n"
        "# This file is a cache directory tag created by pytest.\n"
    )

    @staticmethod
    def _make_pytest_cache(tmp_path: Path) -> Path:
        """Write CACHEDIR.TAG with valid pytest signature into tmp_path."""
        (tmp_path / "CACHEDIR.TAG").write_text(TestCacheOnlyContent._CACHEDIR_TAG)
        return tmp_path

    def test_pure_cache_dir(self, tmp_path):
        """Directory with CACHEDIR.TAG + __pycache__/*.pyc is valid."""
        self._make_pytest_cache(tmp_path)
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "test.cpython-312.pyc").write_text("fake pyc")
        ok, reason = _check_cache_only_content(tmp_path)
        assert ok, reason

    def test_missing_cachedir_tag(self, tmp_path):
        """Directory without CACHEDIR.TAG is rejected."""
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "test.cpython-312.pyc").write_text("fake pyc")
        ok, reason = _check_cache_only_content(tmp_path)
        assert not ok
        assert "CACHEDIR.TAG" in reason

    def test_wrong_cachedir_signature(self, tmp_path):
        """Directory with non-pytest CACHEDIR.TAG is rejected."""
        (tmp_path / "CACHEDIR.TAG").write_text("Signature: something-else")
        ok, reason = _check_cache_only_content(tmp_path)
        assert not ok
        assert "signature" in reason.lower()

    @pytest.mark.parametrize("filename", [
        "readme.txt", "script.py", "data.json", "archive.zip", "data.db",
    ])
    def test_non_cache_files_are_rejected(self, tmp_path, filename):
        """Directory with non-cache file must be rejected."""
        self._make_pytest_cache(tmp_path)
        (tmp_path / filename).write_text("hello")
        ok, reason = _check_cache_only_content(tmp_path)
        assert not ok, f"Should reject {filename}: {reason}"

    def test_mixed_cache_and_non_cache(self, tmp_path):
        """Even one non-cache file should reject the whole dir."""
        self._make_pytest_cache(tmp_path)
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "mod.cpython-312.pyc").write_text("fake")
        (tmp_path / "notes.txt").write_text("secret")
        ok, reason = _check_cache_only_content(tmp_path)
        assert not ok

    def test_pytest_standard_files_accepted(self, tmp_path):
        """README.md, .gitignore, v/cache/nodeids etc. are accepted."""
        self._make_pytest_cache(tmp_path)
        (tmp_path / "README.md").write_text("cache directory")
        (tmp_path / ".gitignore").write_text("*")
        v_cache = tmp_path / "v" / "cache"
        v_cache.mkdir(parents=True)
        (v_cache / "lastfailed").write_text("{}")
        (v_cache / "nodeids").write_text("[]")
        ok, reason = _check_cache_only_content(tmp_path)
        assert ok, reason


# ---------------------------------------------------------------------------
# _is_safe_to_delete
# ---------------------------------------------------------------------------

class TestIsSafeToDelete:
    def test_normal_temp_dir_safe(self):
        # Use a directory under the actual system temp dir
        system_tmp = Path(tempfile.gettempdir()).resolve()
        d = system_tmp / f"mineru_safety_test_{os.getpid()}"
        try:
            d.mkdir(exist_ok=True)
            reason = _is_safe_to_delete(d)
            # Should be None (no safety issue) or a legitimate concern
            if reason is not None and "protected" in reason:
                pytest.fail(f"Temp dir under system temp flagged as protected: {reason}")
        finally:
            if d.exists():
                d.rmdir()

    def test_system_dir_rejected(self):
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        p = Path(system_root)
        if p.exists():
            reason = _is_safe_to_delete(p)
            assert reason is not None, f"SystemRoot {p} should be rejected"
            assert "protected" in reason.lower() or "junction" in reason.lower()

    def test_nonexistent_rejected(self, tmp_path):
        d = tmp_path / "does_not_exist"
        reason = _is_safe_to_delete(d)
        assert reason is not None

    def test_file_not_dir_rejected(self, tmp_path):
        f = tmp_path / "a_file.txt"
        f.write_text("hello")
        reason = _is_safe_to_delete(f)
        assert reason is not None
        assert "not a directory" in reason.lower()


# ---------------------------------------------------------------------------
# _dir_size_and_count
# ---------------------------------------------------------------------------

class TestDirSizeAndCount:
    @pytest.mark.parametrize("setup_fn,expected_size,expected_count", [
        ("empty", 0, 0),
        ("with_files", 150, 2),
        ("nested", 75, 1),
    ])
    def test_dir_size_and_count(self, tmp_path, setup_fn, expected_size, expected_count):
        if setup_fn == "empty":
            pass
        elif setup_fn == "with_files":
            (tmp_path / "a.pyc").write_text("a" * 100)
            (tmp_path / "b.pyc").write_text("b" * 50)
        elif setup_fn == "nested":
            sub = tmp_path / "__pycache__"
            sub.mkdir()
            (sub / "c.pyc").write_text("c" * 75)
        size, count = _dir_size_and_count(tmp_path)
        assert size == expected_size
        assert count == expected_count


# ---------------------------------------------------------------------------
# _scan_stale_workspaces
# ---------------------------------------------------------------------------

class TestScanStaleWorkspaces:
    def test_no_workspaces(self, tmp_path):
        """Scanning a temp dir with no mineru_ dirs returns empty."""
        # Use a subdirectory to avoid actual system temp pollution
        entries = _scan_stale_workspaces(temp_dir=tmp_path)
        assert entries == []

    def test_non_workspace_dir_ignored(self, tmp_path):
        d = tmp_path / "mineru_orphan_deadbeef"
        d.mkdir()
        entries = _scan_stale_workspaces(temp_dir=tmp_path)
        # No marker, so it must be reported but never matched for deletion.
        matched = [e for e in entries if e["verdict"] == "matched"]
        assert len(matched) == 0
        assert len(entries) == 1
        assert entries[0]["verdict"] == "refused"
        assert "unrecognized" in entries[0]["reason"]

    def test_active_workspace_not_matched(self, tmp_path):
        d = tmp_path / "mineru_test_active"
        d.mkdir()
        marker = d / ".mineru-test-workspace.json"
        marker.write_text(json.dumps({
            "schema_version": "1.0",
            "owner": "mineru-test-runner",
            "pid": os.getpid(),
            "repo_root": str(Path.cwd().resolve()),
            "group": "test",
            "created_at": "2026-01-01T00:00:00+00:00",
        }))
        entries = _scan_stale_workspaces(temp_dir=tmp_path)
        matched = [e for e in entries if e["verdict"] == "matched"]
        # PID is alive, should not match for deletion
        refusal_reasons = [e["reason"] for e in entries if e["verdict"] == "refused"]
        assert any("alive" in r for r in refusal_reasons) or len(matched) == 0

    def test_delete_rechecks_and_refuses_unrecognized_workspace(self, tmp_path):
        d = tmp_path / "mineru_untrusted_deadbeef"
        d.mkdir()
        payload = d / "keep.txt"
        payload.write_text("do not delete", encoding="utf-8")
        entry = {
            "path": str(d),
            "name": d.name,
            "type": "new_workspace",
            "verdict": "matched",
            "reason": "stale",
            "deleted": False,
            "bytes_reclaimed": 0,
        }
        _delete_entry(entry)
        assert entry["verdict"] == "refused"
        assert "unrecognized" in entry["reason"]
        assert payload.read_text(encoding="utf-8") == "do not delete"

    def test_delete_verified_stale_workspace(self, tmp_path, monkeypatch):
        from scripts.cleanup_test_caches import ROOT

        monkeypatch.setattr(
            "scripts.cleanup_test_caches._system_temp_dir", lambda: tmp_path,
        )
        monkeypatch.setattr(
            "scripts.test_runtime_workspace._system_temp_dir", lambda: tmp_path,
        )

        d = tmp_path / "mineru_verified_deadbeef"
        d.mkdir()
        marker = {
            "schema_version": "1.0",
            "owner": "mineru-test-runner",
            "pid": 99999999,
            "repo_root": str(ROOT.resolve()),
            "group": "verified",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        (d / ".mineru-test-workspace.json").write_text(
            json.dumps(marker), encoding="utf-8",
        )
        (d / "cache.pyc").write_bytes(b"cache")
        entry = {
            "path": str(d),
            "name": d.name,
            "type": "new_workspace",
            "verdict": "matched",
            "reason": "stale",
            "deleted": False,
            "bytes_reclaimed": 5,
            "size_bytes": 5,
        }
        _delete_entry(entry)
        assert entry["deleted"] is True
        assert not d.exists()


# ---------------------------------------------------------------------------
# _scan_legacy_flattened
# ---------------------------------------------------------------------------

class TestScanLegacyFlattened:
    def test_returns_list(self):
        """Just verify the function runs without crashing."""
        entries = _scan_legacy_flattened()
        assert isinstance(entries, list)

    def test_no_duplicate_paths(self):
        entries = _scan_legacy_flattened()
        paths = [e["path"] for e in entries]
        assert len(paths) == len(set(paths))


# ---------------------------------------------------------------------------
# Legacy flattened simulation (uses tmp_path as fake drive root)
# ---------------------------------------------------------------------------

class TestLegacyFlattenedSimulation:
    """Simulate legacy flattened caches using tmp_path as a fake drive root."""

    def test_cache_only_dir_matched_in_simulation(self, tmp_path):
        """A simulated dir with CACHEDIR.TAG + .pyc files should be identified."""
        temp_dir = Path(tempfile.gettempdir()).resolve()
        flattened_prefix = ""
        resolved_str = str(temp_dir)
        if len(resolved_str) >= 2 and resolved_str[1] == ":":
            resolved_str = resolved_str[2:]
        for part in resolved_str.replace("\\", "/").split("/"):
            if part:
                flattened_prefix += part

        name = f"{flattened_prefix}mineru_fast_catalog_abc123cache"
        d = tmp_path / name
        d.mkdir()
        (d / "CACHEDIR.TAG").write_text(
            "Signature: 8a477f597d28d172789f06886806bc55\n"
        )
        pycache = d / "__pycache__"
        pycache.mkdir()
        (pycache / "test.cpython-312.pyc").write_text("fake")

        ok, reason = _check_cache_only_content(d)
        assert ok, f"Should accept cache-only dir: {reason}"

    def test_with_py_file_rejected(self, tmp_path):
        """Simulated dir with a .py file should be rejected even with CACHEDIR.TAG."""
        temp_dir = Path(tempfile.gettempdir()).resolve()
        flattened_prefix = ""
        resolved_str = str(temp_dir)
        if len(resolved_str) >= 2 and resolved_str[1] == ":":
            resolved_str = resolved_str[2:]
        for part in resolved_str.replace("\\", "/").split("/"):
            if part:
                flattened_prefix += part

        name = f"{flattened_prefix}mineru_fast_security_xyzcache"
        d = tmp_path / name
        d.mkdir()
        (d / "CACHEDIR.TAG").write_text(
            "Signature: 8a477f597d28d172789f06886806bc55\n"
        )
        pycache = d / "__pycache__"
        pycache.mkdir()
        (pycache / "mod.cpython-312.pyc").write_text("fake")
        (d / "config.json").write_text('{"key":"val"}')

        ok, reason = _check_cache_only_content(d)
        assert not ok, f"Should reject dir with .json file: {reason}"
