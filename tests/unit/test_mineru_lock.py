from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import src.mineru_lock as lock_mod


def _use_tmp_lock(monkeypatch, tmp_path: Path) -> Path:
    lock_dir = tmp_path / "locks"
    lock_path = lock_dir / "mineru_convert.lock"
    monkeypatch.setattr(lock_mod, "LOCK_DIR", lock_dir)
    monkeypatch.setattr(lock_mod, "LOCK_PATH", lock_path)
    return lock_path


def _write_lock(path: Path, *, pid: int = 1234, seconds_old: int = 0, paper_number: str = "0000000000000001") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    started_at = (datetime.now() - timedelta(seconds=seconds_old)).isoformat()
    path.write_text(json.dumps({
        "pid": pid,
        "command": "convert_paper_raw_batch.py --all --apply",
        "started_at": started_at,
        "created_at": started_at,
        "updated_at": started_at,
        "paper_number": paper_number,
        "paper_raw_id": paper_number,
        "stage": "run",
        "runner": "cli_api_proxy",
        "api_url": "http://127.0.0.1:8000",
        "backend": "hybrid-engine",
        "method": "auto",
    }), encoding="utf-8")


def test_lock_status_free(monkeypatch, tmp_path):
    _use_tmp_lock(monkeypatch, tmp_path)

    status = lock_mod.read_mineru_lock_status()

    assert status["lock_present"] is False
    assert status["locked"] is False
    assert status["verdict"] == lock_mod.LOCK_FREE


def test_lock_status_owner_dead(monkeypatch, tmp_path):
    lock_path = _use_tmp_lock(monkeypatch, tmp_path)
    _write_lock(lock_path, pid=9999)
    monkeypatch.setattr(lock_mod, "_is_pid_alive", lambda pid: False)

    status = lock_mod.read_mineru_lock_status()

    assert status["lock_present"] is True
    assert status["locked"] is False
    assert status["owner_live"] is False
    assert status["stale"] is True
    assert status["verdict"] == lock_mod.LOCK_OWNER_DEAD


def test_lock_status_stuck_suspected_for_live_old_owner(monkeypatch, tmp_path):
    lock_path = _use_tmp_lock(monkeypatch, tmp_path)
    _write_lock(lock_path, pid=9999, seconds_old=3600)
    monkeypatch.setattr(lock_mod, "_is_pid_alive", lambda pid: True)

    status = lock_mod.read_mineru_lock_status(stuck_warn_seconds=60)

    assert status["locked"] is True
    assert status["owner_live"] is True
    assert status["paper_number"] == "0000000000000001"
    assert status["stage"] == "run"
    assert status["verdict"] == lock_mod.LOCK_STUCK_SUSPECTED


def test_clear_stale_lock_removes_dead_owner_only(monkeypatch, tmp_path):
    lock_path = _use_tmp_lock(monkeypatch, tmp_path)
    _write_lock(lock_path, pid=9999)
    monkeypatch.setattr(lock_mod, "_is_pid_alive", lambda pid: False)

    assert lock_mod.clear_stale_mineru_lock() is True
    assert not lock_path.exists()

    _write_lock(lock_path, pid=os.getpid())
    monkeypatch.setattr(lock_mod, "_is_pid_alive", lambda pid: True)

    assert lock_mod.clear_stale_mineru_lock() is False
    assert lock_path.exists()
