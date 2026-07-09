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

from scripts.agent_acceptance import _pid_state, run_command_with_timeout


pytestmark = pytest.mark.unit


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
        timeout_seconds=30,
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
        timeout_seconds=30,
        check=False,
    )
    assert rc == 3


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
            timeout_seconds=30,
            check=True,
        )
    assert exc_info.value.code == 2
