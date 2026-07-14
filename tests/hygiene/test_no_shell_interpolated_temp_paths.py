"""Hygiene guard: forbid shell-interpolated Windows paths in test runner code.

This test ensures:
1. No ``PYTEST_ADDOPTS`` contains ``cache_dir=`` with a backslash path
2. No ``shell=True`` in agent_acceptance.py or any test runner wrapper
3. No ``bash -lc`` / ``bash -c`` used to launch Python subprocesses

These patterns are the root cause of legacy flattened-root cache pollution:
``shlex.split(posix=True)`` strips backslashes from Windows paths embedded
in environment variables like ``PYTEST_ADDOPTS``, producing malformed paths
like ``C:UsersAdmin...cache`` that create directories on the drive root.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))


# Files that must be checked
PROTECTED_SCRIPTS = [
    "scripts/agent_acceptance.py",
    "scripts/test_runtime_workspace.py",
    "scripts/cleanup_test_caches.py",
]


def _read_script(rel_path: str) -> str:
    return (REPO / rel_path).read_text(encoding="utf-8")


def _find_strings_with_backslash_paths(code: str, file_name: str) -> list[str]:
    """Find string literals that appear to contain Windows paths (drive letter + backslash)."""
    violations: list[str] = []
    try:
        tree = ast.parse(code, filename=file_name)
    except SyntaxError as e:
        return [f"Cannot parse {file_name}: {e}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value
            # Check for Windows paths with backslashes
            if "\\" in s and len(s) > 3:
                # Look for drive-letter paths
                for i, char in enumerate(s):
                    if char == ":" and i > 0 and s[i - 1].isalpha() and i + 1 < len(s):
                        if s[i + 1] == "\\":
                            violations.append(
                                f"{file_name}:{node.lineno}: Windows path in string literal: "
                                f"{s[:120]}"
                            )
                            break
    return violations


def _find_shell_true(code: str, file_name: str) -> list[str]:
    """Find any ``shell=True`` in the code."""
    violations: list[str] = []
    try:
        tree = ast.parse(code, filename=file_name)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "shell":
            if isinstance(node.value, ast.Constant) and node.value.value is True:
                violations.append(
                    f"{file_name}:{node.lineno}: shell=True found"
                )
    return violations


def _find_bash_invocation(code: str, file_name: str) -> list[str]:
    """Find ``bash -lc`` or ``bash -c`` in string literals or lists."""
    violations: list[str] = []
    try:
        tree = ast.parse(code, filename=file_name)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value
            if "bash -lc" in s or "bash -c " in s:
                violations.append(
                    f"{file_name}:{node.lineno}: bash inline command: {s[:120]}"
                )
        # Also check list elements
        if isinstance(node, ast.List):
            for elt in node.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    if elt.value in ("bash", "sh", "/bin/bash", "/bin/sh"):
                        violations.append(
                            f"{file_name}:{elt.lineno}: bash/sh as list element"
                        )
    return violations


def _find_pytest_addopts_with_windows_path(code: str, file_name: str) -> list[str]:
    """Find PYTEST_ADDOPTS assignment with a cache_dir= containing backslashes.

    This is the SPECIFIC root cause — PYTEST_ADDOPTS with a Windows path
    gets mangled by shlex.split(posix=True) used inside pytest.
    """
    violations: list[str] = []
    try:
        tree = ast.parse(code, filename=file_name)
    except SyntaxError:
        return []

    for node in ast.walk(tree):
        # Check f-string and string assignments containing PYTEST_ADDOPTS
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value
            if "PYTEST_ADDOPTS" in s and "cache_dir=" in s:
                violations.append(
                    f"{file_name}:{node.lineno}: PYTEST_ADDOPTS with cache_dir= "
                    f"in string literal — path may be mangled by shlex: "
                    f"{s[:120]}"
                )
        # Check dict assignments
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "PYTEST_ADDOPTS":
                    violations.append(
                        f"{file_name}:{key.lineno}: PYTEST_ADDOPTS used as dict key — "
                        f"ensure no Windows paths in the value"
                    )

    return violations


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNoShellInterpolatedTempPaths:
    def test_no_shell_true_in_protected_scripts(self):
        """agent_acceptance.py and friends must not use shell=True."""
        all_violations = []
        for script in PROTECTED_SCRIPTS:
            if not (REPO / script).exists():
                continue
            code = _read_script(script)
            all_violations.extend(_find_shell_true(code, script))
        assert not all_violations, (
            "shell=True found in protected scripts:\n" + "\n".join(all_violations)
        )

    def test_no_bash_in_protected_scripts(self):
        """agent_acceptance.py and friends must not use bash inline."""
        all_violations = []
        for script in PROTECTED_SCRIPTS:
            if not (REPO / script).exists():
                continue
            code = _read_script(script)
            all_violations.extend(_find_bash_invocation(code, script))
        assert not all_violations, (
            "bash invocation found in protected scripts:\n" + "\n".join(all_violations)
        )

    def test_no_pytest_addopts_with_windows_paths(self):
        """PYTEST_ADDOPTS must NOT contain paths — use env vars + --basetemp instead."""
        all_violations = []
        for script in PROTECTED_SCRIPTS:
            if not (REPO / script).exists():
                continue
            code = _read_script(script)
            all_violations.extend(_find_pytest_addopts_with_windows_path(code, script))
        assert not all_violations, (
            "PYTEST_ADDOPTS with paths found — use TestRuntimeWorkspace.child_env() instead:\n"
            + "\n".join(all_violations)
        )

    def test_no_path_replace_stripping_in_test_runner(self):
        """Test runner code must not use replace('\\\\', '') on execution paths."""
        # Check that path manipulation functions only appear in display/log contexts
        for script in PROTECTED_SCRIPTS:
            if not (REPO / script).exists():
                continue
            code = _read_script(script)
            if 'replace("\\\\", "")' in code or "replace('\\\\', '')" in code:
                pytest.fail(
                    f"{script}: replace('\\\\', '') found — backslash "
                    f"stripping on paths can cause pollution"
                )
