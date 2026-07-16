"""Unit tests for acceptance runner zombie detection (Phase 0.7).

Verifies that ``_pid_state`` correctly distinguishes:
- zombie processes (POSIX ``Z`` state) from alive processes
- ``PermissionError`` means alive (not dead)
- the kill cleanup runs in ``finally`` even on ``BaseException``
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.agent_acceptance import _pid_state, run_command_with_timeout


pytestmark = pytest.mark.unit


def test_pid_state_returns_dead_for_nonexistent_pid():
    """A PID that does not exist must return 'dead'."""
    # Use a PID that is very unlikely to exist.
    fake_pid = 999999
    state = _pid_state(fake_pid)
    assert state == "dead", f"PID {fake_pid} should be dead, got {state}"


@pytest.mark.skipif(os.name == "nt", reason="POSIX /proc test")
def test_pid_state_detects_zombie_on_posix():
    """On POSIX, a zombie process (state 'Z') must return 'zombie', not 'alive'.

    We mock /proc/<pid>/stat to return a zombie state string.
    """
    pid = 42
    fake_stat = f"{pid} (python) Z\n"

    with patch("pathlib.Path.exists", return_value=True):
        with patch("pathlib.Path.read_text", return_value=fake_stat):
            state = _pid_state(pid)
    assert state == "zombie", f"Zombie process should return 'zombie', got {state}"


@pytest.mark.skipif(os.name == "nt", reason="POSIX /proc test")
def test_pid_state_detects_alive_on_posix():
    """On POSIX, a running process (state 'R') must return 'alive'."""
    pid = 42
    fake_stat = f"{pid} (python) R\n"

    with patch("pathlib.Path.exists", return_value=True):
        with patch("pathlib.Path.read_text", return_value=fake_stat):
            state = _pid_state(pid)
    assert state == "alive", f"Running process should return 'alive', got {state}"


@pytest.mark.skipif(os.name == "nt", reason="POSIX fallback test")
def test_pid_state_permissionerror_means_alive():
    """PermissionError from os.kill(pid, 0) means the process exists — alive.

    BEFORE fix: PermissionError was caught alongside ProcessLookupError and
    treated as 'dead', which is wrong — the process is alive but owned by
    another user.
    """
    pid = 42

    # Mock /proc to not exist, so we fall back to os.kill
    with patch("pathlib.Path.exists", return_value=False):
        with patch("os.kill", side_effect=PermissionError("not allowed")):
            state = _pid_state(pid)
    assert state == "alive", (
        f"PermissionError should mean alive, got {state}"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX fallback test")
def test_pid_state_processlookuperror_means_dead():
    """ProcessLookupError from os.kill(pid, 0) means the process is gone."""
    pid = 42

    with patch("pathlib.Path.exists", return_value=False):
        with patch("os.kill", side_effect=ProcessLookupError("no such process")):
            state = _pid_state(pid)
    assert state == "dead"


def test_pid_state_current_process_is_alive():
    """The current process must be detected as alive."""
    state = _pid_state(os.getpid())
    assert state == "alive", f"Current process should be alive, got {state}"


def test_runner_kill_runs_in_finally_on_keyboard_interrupt(tmp_path: Path):
    """When polling is interrupted by KeyboardInterrupt (a BaseException), the
    process tree must still be killed because cleanup is in ``finally``.

    The runner polls ``proc.poll()`` in a loop (not ``proc.wait()``), so the
    interrupt is injected via ``poll.side_effect`` to match the real control
    flow. BEFORE fix: kill was in ``except TimeoutExpired`` only, so
    BaseException bypassed cleanup, leaving orphan processes.
    """
    import subprocess

    # Create a mock proc whose second poll() raises KeyboardInterrupt (the
    # first poll returns None so the loop body executes once, then the
    # interrupt fires during polling — exactly how a real Ctrl-C lands).
    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.pid = 99999
    mock_proc.poll.side_effect = [None, KeyboardInterrupt("simulated interrupt")]
    mock_proc.wait.return_value = 0
    mock_proc.returncode = 0

    with patch("subprocess.Popen", return_value=mock_proc):
        with patch("scripts.agent_acceptance._kill_process_tree") as mock_kill:
            with patch("scripts.agent_acceptance._descendant_pids", return_value=[]):
                with pytest.raises(KeyboardInterrupt):
                    run_command_with_timeout(
                        [sys.executable, "-c", "pass"],
                        timeout_seconds=5,
                        check=False,
                    )
                # _kill_process_tree MUST have been called even though
                # the exception was KeyboardInterrupt (BaseException).
                mock_kill.assert_called_once()




