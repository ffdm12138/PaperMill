from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from filelock import FileLock
import pytest

from src.discovery.audits.reset_state import (
    audit_reset_state,
    probe_existing_file_lock,
    resolve_safe_report_path,
)


def _empty_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    (root / "paper_raw").mkdir(parents=True)
    (root / "paper_raw" / ".gitkeep").write_text("", encoding="utf-8")
    (root / "papers").mkdir()
    (root / "catalog").mkdir()
    (root / "catalog" / "paper_number_ledger.json").write_text(json.dumps({
        "schema_version": "2.0",
        "max_number": "0000000000000000",
        "items": {},
    }), encoding="utf-8")
    return root


def test_reset_audit_reports_all_independent_findings(tmp_path: Path):
    root = _empty_root(tmp_path)
    notebooks = root / "discovery" / "keyword_notebooks"
    notebooks.mkdir(parents=True)
    (notebooks / "broken.json").write_text("{broken", encoding="utf-8")
    mystery = root / "discovery" / "locks" / "mystery.lock"
    mystery.parent.mkdir(parents=True)
    mystery.write_bytes(b"")
    raw_workspace = root / "paper_raw" / "0000000000000001"
    raw_workspace.mkdir()
    (raw_workspace / "asset.txt").write_text("leftover", encoding="utf-8")

    report = audit_reset_state(data_root=root, expected_formal_count=0)
    codes = {item["code"] for item in report["findings"]}
    assert "notebook_unreadable" in codes
    assert "unknown_lock" in codes
    assert "paper_raw_not_empty" in codes
    assert report["fresh_discovery_readiness"] in {"REPAIR_REQUIRED", "RESET_REQUIRED"}


def test_active_filelock_is_active_not_unverifiable(tmp_path: Path):
    root = _empty_root(tmp_path)
    lock_path = root / "transactions" / "locks" / "ledger.lock"
    lock_path.parent.mkdir(parents=True)
    lock = FileLock(str(lock_path))
    lock.acquire()
    try:
        probe = probe_existing_file_lock(lock_path)
        assert probe.classification == "active"
        report = audit_reset_state(data_root=root, expected_formal_count=0)
        assert any(
            item["code"] == "active_lock" and item["severity"] == "block"
            for item in report["findings"]
        )
        assert report["fresh_discovery_readiness"] == "BLOCKED_BY_ACTIVE_TRANSACTION"
    finally:
        lock.release()


def test_completed_history_does_not_block_empty_audit(tmp_path: Path):
    root = _empty_root(tmp_path)
    completed = root / "transactions" / "commit" / "completed"
    completed.mkdir(parents=True)
    (completed / "history.json").write_text(json.dumps({"phase": "complete"}), encoding="utf-8")
    report = audit_reset_state(data_root=root, expected_formal_count=0)
    locks = report["locks_and_transactions"]
    assert locks["commit_journals"] == []
    assert "history.json" in locks["commit_history"]


def test_root_gitkeep_is_not_paper_raw_pollution(tmp_path: Path):
    root = _empty_root(tmp_path)
    report = audit_reset_state(data_root=root, expected_formal_count=0)
    assert report["paper_raw"]["orphan_files"] == []
    assert not any(
        item["code"] == "paper_raw_not_empty" for item in report["findings"]
    )


def test_unknown_notebook_and_pending_members_are_reported(tmp_path: Path):
    root = _empty_root(tmp_path)
    notebook_root = root / "discovery" / "keyword_notebooks"
    notebook_root.mkdir(parents=True)
    (notebook_root / "leftover.tmp").write_text("x", encoding="utf-8")
    pending = root / "discovery" / "pending_pages"
    pending.mkdir(parents=True)
    (pending / "leftover.tmp").write_text("x", encoding="utf-8")
    report = audit_reset_state(data_root=root, expected_formal_count=0)
    codes = {item["code"] for item in report["findings"]}
    assert "notebook_unknown_member" in codes
    assert "page_journal_unknown_member" in codes


def test_lock_probe_does_not_change_file_bytes_or_mtime(tmp_path: Path):
    path = tmp_path / "probe.lock"
    path.write_bytes(b"existing-lock-content")
    before = (path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes())
    probe = probe_existing_file_lock(path)
    after = (path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes())
    assert probe.classification == "stale"
    assert after == before


def test_report_path_cannot_escape_or_reenter_audited_root(tmp_path: Path):
    root = _empty_root(tmp_path)
    with pytest.raises(ValueError, match="outside audited root"):
        resolve_safe_report_path(root / "report.json", audited_root=root)
    safe = resolve_safe_report_path(tmp_path / "report.json", audited_root=root)
    assert safe == (tmp_path / "report.json").absolute()

    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    link = tmp_path / "report-link.json"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable on this platform")
    with pytest.raises(ValueError, match="symlink/reparse"):
        resolve_safe_report_path(link, audited_root=root)


def test_root_gitkeep_does_not_count_as_paper_raw_pollution_for_empty_expected_zero(tmp_path: Path):
    root = _empty_root(tmp_path)
    report = audit_reset_state(data_root=root, expected_formal_count=0)
    assert report["paper_raw"]["orphan_files"] == []
    assert report["zero_write_evidence"]["zero_write"] is True


def test_reset_audit_cli_is_cwd_independent_and_report_safe(tmp_path: Path):
    root = _empty_root(tmp_path)
    script = Path(__file__).resolve().parents[3] / "scripts" / "audit_discovery_reset_state.py"
    repo_root = script.parents[1]
    other_cwd = tmp_path / "other-cwd"
    other_cwd.mkdir()
    environment = os.environ.copy()
    environment.update({
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    })
    invocations = [
        ([sys.executable, str(script)], tmp_path),
        ([sys.executable, "-m", "scripts.audit_discovery_reset_state"], repo_root),
        ([sys.executable, str(script)], other_cwd),
    ]
    summaries = []
    for command, cwd in invocations:
        completed = subprocess.run(
            command + ["--data-root", str(root), "--expected-formal-count", "0"],
            cwd=str(cwd), env=environment, shell=False,
            capture_output=True, text=True, encoding="utf-8",
        )
        assert completed.returncode == 3
        value = json.loads(completed.stdout)
        summaries.append((
            value["fresh_discovery_readiness"],
            sorted(item["code"] for item in value["findings"]),
            value["zero_write_evidence"]["zero_write"],
        ))
    assert summaries[0] == summaries[1] == summaries[2]

    report_path = tmp_path / "reports" / "audit.json"
    completed = subprocess.run(
        invocations[0][0] + [
            "--data-root", str(root), "--expected-formal-count", "0",
            "--json-report", str(report_path),
        ],
        cwd=str(tmp_path), env=environment, shell=False,
        capture_output=True, text=True, encoding="utf-8",
    )
    assert completed.returncode == 3
    assert json.loads(report_path.read_text(encoding="utf-8"))["zero_write_evidence"]["zero_write"] is True

    completed = subprocess.run(
        invocations[0][0] + [
            "--data-root", str(root), "--expected-formal-count", "0",
            "--json-report", str(root / "report.json"),
        ],
        cwd=str(tmp_path), env=environment, shell=False,
        capture_output=True, text=True, encoding="utf-8",
    )
    assert completed.returncode == 2
    assert not (root / "report.json").exists()

    escape = tmp_path / "escape"
    try:
        os.symlink(root, escape, target_is_directory=True)
    except (OSError, NotImplementedError):
        return
    completed = subprocess.run(
        invocations[0][0] + [
            "--data-root", str(root), "--expected-formal-count", "0",
            "--json-report", str(escape / "report.json"),
        ],
        cwd=str(tmp_path), env=environment, shell=False,
        capture_output=True, text=True, encoding="utf-8",
    )
    assert completed.returncode == 2
    assert not (root / "report.json").exists()
