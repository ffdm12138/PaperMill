"""Unit tests for the unified acceptance runner (Phase 6).

Verifies ``run_command_with_timeout``:
- returns the correct exit code on normal completion
- kills the full process tree on timeout (including descendants)
- returns 124 on timeout when check=False
- a descendant subprocess is terminated when the parent times out
"""
from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

from scripts.agent_acceptance import PYTEST_PREFIX, _pid_state, run_command_with_timeout


pytestmark = pytest.mark.unit


def test_pytest_prefix_disables_repo_cache_provider():
    assert PYTEST_PREFIX[-2:] == ["-p", "no:cacheprovider"]


def _write_script(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_runner_normal_exit_zero(tmp_path: Path):
    script = _write_script(tmp_path / "ok.py", """
        import sys
        print("hello")
        sys.exit(0)
    """)
    rc = run_command_with_timeout(
        [sys.executable, str(script)],
        timeout_seconds=5,
        check=False,
    )
    assert rc == 0


def test_runner_normal_exit_nonzero(tmp_path: Path):
    script = _write_script(tmp_path / "fail.py", """
        import sys
        sys.exit(3)
    """)
    rc = run_command_with_timeout(
        [sys.executable, str(script)],
        timeout_seconds=5,
        check=False,
    )
    assert rc == 3


def test_runner_large_output_does_not_deadlock(tmp_path: Path):
    script = _write_script(tmp_path / "large.py", """
        print("x" * 1000)
        for _ in range(2000):
            print("payload")
    """)
    assert run_command_with_timeout(
        [sys.executable, str(script)], timeout_seconds=10, check=False
    ) == 0


def test_runner_returns_when_descendant_keeps_output_handle(tmp_path: Path):
    pid_file = tmp_path / "orphan_pid.txt"
    child = _write_script(tmp_path / "orphan.py", f"""
        import os, time
        with open({str(pid_file)!r}, "w") as fh:
            fh.write(str(os.getpid()))
        time.sleep(600)
    """)
    parent = _write_script(tmp_path / "parent_exits.py", f"""
        import subprocess, sys, time
        subprocess.Popen([sys.executable, {str(child)!r}])
        time.sleep(0.3)
        print("parent passed", flush=True)
    """)
    assert run_command_with_timeout(
        [sys.executable, str(parent)], timeout_seconds=5, check=False
    ) == 0
    if pid_file.exists():
        assert _pid_state(int(pid_file.read_text())) in ("dead", "zombie")


def test_runner_timeout_kills_process_tree(tmp_path: Path):
    """A script that sleeps forever must be killed and return 124."""
    script = _write_script(tmp_path / "hang.py", """
        import time
        print("hanging", flush=True)
        time.sleep(600)
    """)
    rc = run_command_with_timeout(
        [sys.executable, str(script)],
        timeout_seconds=5,
        check=False,
    )
    assert rc == 124


def test_runner_timeout_kills_descendant(tmp_path: Path):
    """A descendant subprocess must be killed when the parent times out.

    The parent spawns a child (via a separate script file) that writes its PID
    to a file and then sleeps. After the runner times out and kills the tree,
    the child must no longer be running.
    """
    pid_file = tmp_path / "child_pid.txt"
    child_script = _write_script(tmp_path / "child.py", f"""
        import os
        import time
        with open({str(pid_file)!r}, "w") as fh:
            fh.write(str(os.getpid()))
        time.sleep(600)
    """)
    parent_script = _write_script(tmp_path / "parent.py", f"""
        import subprocess
        import sys
        import time
        subprocess.Popen([sys.executable, {str(child_script)!r}])
        time.sleep(600)
    """)
    rc = run_command_with_timeout(
        [sys.executable, str(parent_script)],
        timeout_seconds=8,
        check=False,
    )
    assert rc == 124
    # The descendant must be dead or zombie (not alive). If we got a PID,
    # check it using the _pid_state helper which correctly distinguishes
    # zombie (Z state) from alive on POSIX, and uses STILL_ACTIVE on Windows.
    if pid_file.exists():
        child_pid = int(pid_file.read_text().strip())
        state = _pid_state(child_pid)
        assert state in ("dead", "zombie"), (
            f"descendant pid {child_pid} still alive (state={state}) after timeout kill"
        )


def test_runner_check_true_exits_on_nonzero(tmp_path: Path):
    """When check=True, a non-zero exit must sys.exit (not return)."""
    script = _write_script(tmp_path / "fail.py", """
        import sys
        sys.exit(2)
    """)
    with pytest.raises(SystemExit) as exc_info:
        run_command_with_timeout(
            [sys.executable, str(script)],
            timeout_seconds=5,
            check=True,
        )
    assert exc_info.value.code == 2
