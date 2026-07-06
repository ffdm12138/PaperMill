"""Tests for scripts/audit_paper_raw_duplicate_workspaces.py.

Slimmed to the five core safety behaviors: dry-run is non-destructive,
--apply-cleanup quarantines the duplicate and marks the ledger, the veto
(refuse to drop a strictly-higher-stage workspace), idempotency, and
--strict exits nonzero on pending drops. Removed: keep-rule ranking details
and equal-rank tie-break variants (the tie-break is still exercised indirectly
by the apply/idempotent tests).

All tests operate on synthetic tmp trees only — they never touch the real
``data/paper_raw`` or ``data/catalog`` ledger.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.helpers.paper_raw_factory import make_legacy_workspace, make_staged_source

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "audit_paper_raw_duplicate_workspaces.py"
PY = sys.executable


def _run_audit(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run the audit script with a timeout.

    Returns a CompletedProcess with returncode=124 on timeout so the test
    fails with a clear message instead of hanging the whole suite.
    """
    import os
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        return subprocess.run([PY, str(SCRIPT), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        stderr = exc.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        stderr = (stderr or "") + f"\n[TIMEOUT] audit script did not finish within {timeout}s"
        return subprocess.CompletedProcess(
            args=exc.cmd or [PY, str(SCRIPT), *args],
            returncode=124,
            stdout=stdout or "",
            stderr=stderr,
        )


def _ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "catalog" / "paper_number_ledger.json"


def _write_ledger_item(tmp_path: Path, number: str, folder: Path, *, state: str = "reserved") -> None:
    """Write/update a ledger item for a numbered workspace at a given state."""
    ledger_path = _ledger_path(tmp_path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    if not ledger_path.exists():
        ledger_path.write_text(json.dumps({"schema_version": "1.0", "max_number": number, "items": {}}, indent=2), encoding="utf-8")
    data = json.loads(ledger_path.read_text(encoding="utf-8"))
    data["items"][number] = {
        "folder_name": folder.name,
        "folder_path": str(folder),
        "planned_paper_id": "",
        "state": state,
        "created_at": "2026-01-01T00:00:00",
    }
    ledger_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _dup_group_number(tmp_path: Path, legacy_bytes: bytes, *, numbered_state: str = "reserved",
                     legacy_import_status: str = "metadata_resolve_failed",
                     numbered_import_status: str = "ready_for_convert") -> tuple[Path, Path, Path]:
    """Build a paper_raw tree where a numbered workspace and a legacy one share a PDF."""
    paper_raw = tmp_path / "paper_raw"
    legacy = make_legacy_workspace(tmp_path, pdf_bytes=legacy_bytes, doi="10.1/dup",
                                   import_status=legacy_import_status)
    numbered = paper_raw / "0000000000000228"
    numbered.mkdir(parents=True)
    (numbered / "0000000000000228.pdf").write_bytes(legacy_bytes)
    (numbered / "0000000000000228.md").write_text("# numbered dup", encoding="utf-8")
    # minimal metadata so is_paper_raw_workspace / identity resolve
    (numbered / "0000000000000228.metadata.json").write_text(
        json.dumps({"paper_number": "0000000000000228", "paper_raw_id": "0000000000000228", "identifiers": {"doi": "10.1/dup"}},
                   ensure_ascii=False), encoding="utf-8")
    # write import_status for the numbered workspace
    from src.services.ingest_state import write_import_status
    write_import_status(numbered, numbered_import_status, reason="restaged", extra={"source_id": "0000000000000228"})
    _write_ledger_item(tmp_path, "0000000000000228", numbered, state=numbered_state)
    return paper_raw, legacy, numbered


def test_dry_run_report_does_not_modify_workspaces(tmp_path):
    """build_report (dry-run) identifies the duplicate without moving or deleting anything."""
    paper_raw, legacy, numbered = _dup_group_number(tmp_path, legacy_bytes=b"%PDF same")

    from scripts import audit_paper_raw_duplicate_workspaces as A
    rep = A.build_report(paper_raw_dir=paper_raw, ledger_path=_ledger_path(tmp_path))

    assert rep["duplicate_group_count"] == 1
    group = rep["groups"][0]
    assert Path(group["keep"]["folder"]).name == "1979_sykest_untitled"
    assert Path(group["drop"][0]["folder"]).name == "0000000000000228"
    # dry-run must not modify, move, or quarantine any workspace
    assert legacy.is_dir() and numbered.is_dir()
    assert not (paper_raw / "quarantine").exists()


def test_apply_cleanup_moves_to_quarantine_and_marks_ledger(tmp_path):
    paper_raw, legacy, numbered = _dup_group_number(tmp_path, legacy_bytes=b"%PDF cleanup")

    rc = _run_audit(["--paper-raw-dir", str(paper_raw), "--ledger-path", str(_ledger_path(tmp_path)), "--apply-cleanup"])
    assert rc.returncode == 0, rc.stderr

    # numbered folder moved into quarantine holding dir
    quarantine_target = paper_raw / "quarantine" / "duplicate_workspaces" / "0000000000000228"
    assert quarantine_target.is_dir(), "dropped folder should be moved into quarantine"
    assert not numbered.exists(), "original numbered folder should no longer exist at its place"

    # import_status rewritten
    status = json.loads((quarantine_target / ".import_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "quarantined_duplicate"
    assert status["duplicate_of"] == "1979_sykest_untitled"

    # ledger entry marked + repointed, max_number untouched
    ledger = json.loads(_ledger_path(tmp_path).read_text(encoding="utf-8"))
    item = ledger["items"]["0000000000000228"]
    assert item["state"] == "quarantined_duplicate"
    assert "quarantine" in item["folder_path"]
    assert ledger["max_number"] == "0000000000000228"  # never decremented

    # legacy folder untouched
    assert legacy.is_dir()


def test_apply_cleanup_refuses_to_drop_strictly_higher_stage_than_keep(tmp_path):
    """Veto: a drop whose rank is STRICTLY higher than the keep's must abort.
    Same-rank duplicates are no longer vetoed (resolved by tie-break); only a
    strictly-higher-stage drop is refused.

    To exercise the veto directly we hand _apply_cleanup a crafted report where
    the keep is a low-rank workspace and the drop is a strictly higher-rank
    workspace in the same paper_raw tree (so the keep/drop folders exist)."""
    from src.services.ingest_state import write_import_status
    paper_raw = tmp_path / "paper_raw"
    keep = paper_raw / "0000000000000228"     # ready_for_convert (rank 15)
    drop = paper_raw / "1979_sykest_untitled"  # committed (rank 60) - STRICTLY higher
    for f, status in ((keep, "ready_for_convert"), (drop, "committed")):
        f.mkdir(parents=True)
        (f / "x.pdf").write_bytes(b"%PDF veto")
        (f / "x.md").write_text("# x", encoding="utf-8")
        (f / "x.metadata.json").write_text("{}", encoding="utf-8")
        write_import_status(f, status, reason="test")

    crafted = {
        "groups": [{
            "evidence": {"pdf_sha256": "x"},
            "duplicate_reason": "pdf_sha256_duplicate",
            "keep": {"folder": str(keep), "paper_number": "0000000000000228",
                     "paper_raw_id": "0000000000000228", "state": "",
                     "import_status": "ready_for_convert", "asset_count": 1,
                     "has_paper_number_marker": False},
            "drop": [{"folder": str(drop), "paper_number": "0000000000000157",
                      "paper_raw_id": "1979_sykest_untitled", "state": "",
                      "import_status": "committed", "asset_count": 1,
                      "has_paper_number_marker": True}],
        }],
    }

    from scripts import audit_paper_raw_duplicate_workspaces as A
    keep_rank = A._rank_from_dict(crafted["groups"][0]["keep"])
    drop_rank = A._rank_from_dict(crafted["groups"][0]["drop"][0])
    assert drop_rank > keep_rank, "test setup must force a strictly-higher drop"
    with pytest.raises(RuntimeError):
        A._apply_cleanup(crafted, paper_raw_dir=paper_raw, ledger_path=_ledger_path(tmp_path))


def test_apply_cleanup_idempotent(tmp_path):
    paper_raw, legacy, numbered = _dup_group_number(tmp_path, legacy_bytes=b"%PDF idem")

    first = _run_audit(["--paper-raw-dir", str(paper_raw), "--ledger-path", str(_ledger_path(tmp_path)), "--apply-cleanup"])
    assert first.returncode == 0
    # second run: no candidates left (legacy kept, numbered quarantined & excluded)
    second = _run_audit(["--paper-raw-dir", str(paper_raw), "--ledger-path", str(_ledger_path(tmp_path)), "--strict"])
    assert second.returncode == 0, second.stdout + second.stderr


def test_strict_exits_nonzero_on_pending_drops(tmp_path):
    paper_raw, legacy, numbered = _dup_group_number(tmp_path, legacy_bytes=b"%PDF strict")

    rc = _run_audit(["--paper-raw-dir", str(paper_raw), "--ledger-path", str(_ledger_path(tmp_path)), "--strict"])
    assert rc.returncode == 1
