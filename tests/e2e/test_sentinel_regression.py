"""Sentinel-file regression tests proving P0 bug fixes.

These tests exercise the path re-validation gates added to ``commit.py``,
``rollback.py``, and ``transaction_paths.py``.  Each test simulates a
malicious / corrupted / stale journal that would have resulted in data loss
before the fix and asserts that the safety layer rejects it.
"""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from src.services.transaction_paths import (
    check_destructive_path,
    validate_commit_journal,
    validate_rollback_journal,
)

pytestmark = pytest.mark.e2e


def _make_commit_journal(tmp: Path, tx_id: str, data: dict) -> Path:
    """Write a commit journal under ``tmp/commit/{tx_id}.json``."""
    path = tmp / "commit" / f"{tx_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def _make_rollback_journal(tmp: Path, tx_id: str, data: dict) -> Path:
    """Write a rollback journal under ``tmp/rollback/{tx_id}.json``."""
    path = tmp / "rollback" / f"{tx_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def _common(tx_id: str, pnum: str = "0000000000000001") -> dict:
    return {
        "paper_number": pnum,
        "paper_id": "2024_Smith_test",
        "transaction_id": tx_id,
        "created_at": "2025-01-01T00:00:00",
    }


# ── Sentinel: rollback formal_quarantine outside papers root ────────────


def test_rollback_rejects_quarantine_outside_papers_root(
    tmp_path: Path,
) -> None:
    """If a rollback journal points ``formal_quarantine`` outside
    ``papers_root``, ``validate_rollback_journal`` raises containment error.

    Before the fix, the rollback logic trusted journal paths without
    re-validation, so a corrupted journal could cause ``shutil.rmtree``
    on an arbitrary sibling directory.
    """
    paper_raw_root = tmp_path / "paper_raw"
    papers_root = tmp_path / "papers"
    tx_root = tmp_path / "transactions"
    pnum = "0000000000000001"
    pid = "2024_Smith_test"
    (paper_raw_root / pnum).mkdir(parents=True)
    (papers_root / pid).mkdir(parents=True)
    tx_id = str(uuid4())

    j = _common(tx_id, pnum)
    j["phase"] = "quarantine"
    j["formal_path"] = str(papers_root / pid)
    j["raw_path"] = str(paper_raw_root / pnum)
    j["staging_path"] = str(paper_raw_root / f".rollback_{pnum}_{tx_id}")
    # quarantine pointing OUTSIDE papers_root
    j["formal_quarantine"] = str(tmp_path / "outside_papers" / "malicious")

    journal_path = _make_rollback_journal(tx_root, tx_id, j)

    with pytest.raises(Exception) as exc:
        validate_rollback_journal(
            journal=j,
            journal_path=journal_path,
            paper_raw_root=paper_raw_root,
            papers_root=papers_root,
            transaction_root=tx_root,
        )
    msg = str(exc.value).lower()
    assert "containment" in msg or "outside" in msg or "not under" in msg


# ── Sentinel: commit staging_path outside papers_root ───────────────────


def test_commit_recovery_does_not_replace_unrelated_staging(
    tmp_path: Path,
) -> None:
    """If a commit journal's ``staging_path`` resolves outside
    ``papers_root``, ``validate_commit_journal`` rejects it.

    Before the fix, a recovered commit workflow could blindly ``os.replace``
    or ``shutil.rmtree`` on the path stored in the journal, even if that
    path pointed into an unrelated workspace.
    """
    paper_raw_root = tmp_path / "paper_raw"
    papers_root = tmp_path / "papers"
    tx_root = tmp_path / "transactions"
    pnum = "0000000000000001"
    pid = "2024_Smith_test"
    (paper_raw_root / pnum).mkdir(parents=True)
    (papers_root / pid).mkdir(parents=True)
    tx_id = str(uuid4())

    # staging_path outside papers_root
    outside_staging = tmp_path / "unrelated_staging"
    outside_staging.mkdir()

    j = _common(tx_id, pnum)
    j["phase"] = "staging"
    j["source_workspace"] = str(paper_raw_root / pnum)
    j["staging_path"] = str(outside_staging)  # outside papers_root
    j["final_path"] = str(papers_root / pid)

    journal_path = _make_commit_journal(tx_root, tx_id, j)

    with pytest.raises(Exception) as exc:
        validate_commit_journal(
            journal=j,
            journal_path=journal_path,
            paper_raw_root=paper_raw_root,
            papers_root=papers_root,
            transaction_root=tx_root,
        )
    msg = str(exc.value).lower()
    assert "containment" in msg or "outside" in msg or "not under" in msg


# ── Sentinel: rollback staging outside paper_raw_root ───────────────────


def test_rollback_rejects_staging_outside_paper_raw(tmp_path: Path) -> None:
    """A rollback journal with ``staging_path`` outside ``paper_raw_root``
    must be rejected."""
    paper_raw_root = tmp_path / "paper_raw"
    papers_root = tmp_path / "papers"
    tx_root = tmp_path / "transactions"
    pnum = "0000000000000001"
    pid = "2024_Smith_test"
    (paper_raw_root / pnum).mkdir(parents=True)
    (papers_root / pid).mkdir(parents=True)
    tx_id = str(uuid4())

    outside_staging = tmp_path / "outside_staging"
    outside_staging.mkdir()

    j = _common(tx_id, pnum)
    j["phase"] = "quarantine"
    j["formal_path"] = str(papers_root / pid)
    j["raw_path"] = str(paper_raw_root / pnum)
    j["staging_path"] = str(outside_staging)  # outside paper_raw_root
    j["formal_quarantine"] = str(papers_root / f".{pid}.rollback_{tx_id}")

    journal_path = _make_rollback_journal(tx_root, tx_id, j)

    with pytest.raises(Exception) as exc:
        validate_rollback_journal(
            journal=j,
            journal_path=journal_path,
            paper_raw_root=paper_raw_root,
            papers_root=papers_root,
            transaction_root=tx_root,
        )
    msg = str(exc.value).lower()
    assert "containment" in msg or "outside" in msg or "not under" in msg


# ── Sentinel: check_destructive_path rejects root identity ──────────────


def test_check_destructive_rejects_root(tmp_path: Path) -> None:
    """``check_destructive_path`` must reject a candidate equal to root."""
    root = tmp_path.resolve()
    with pytest.raises(Exception) as exc:
        check_destructive_path(root, root, field="candidate")
    assert "root" in str(exc.value).lower()


# ── Sentinel: check_destructive_path rejects symlink chain ──────────────


def test_check_destructive_rejects_symlink(tmp_path: Path) -> None:
    """``check_destructive_path`` must reject a candidate with a symlink
    in the chain."""
    real_dir = tmp_path / "real_target"
    real_dir.mkdir()
    link = tmp_path / "link_to_real"
    link.symlink_to(real_dir, target_is_directory=True)
    target = link / "child"
    target.mkdir()

    with pytest.raises(Exception) as exc:
        check_destructive_path(tmp_path, target, field="target")
    assert "symlink" in str(exc.value).lower()


# ── Sentinel: paper_number vs source_workspace basename mismatch ────────


def test_pre_destruction_path_verification_fails_on_mismatch(
    tmp_path: Path,
) -> None:
    """If a commit journal's ``paper_number`` doesn't match its
    ``source_workspace`` basename, the validator must raise."""
    paper_raw_root = tmp_path / "paper_raw"
    pnum = "0000000000000001"
    (paper_raw_root / pnum).mkdir(parents=True)
    tx_id = str(uuid4())

    # source_workspace basename "0000000000000002" but paper_number "0000000000000001"
    wrong_source = paper_raw_root / "0000000000000002"
    wrong_source.mkdir()

    j = _common(tx_id, pnum)
    j["phase"] = "staging"
    j["source_workspace"] = str(wrong_source)
    j["staging_path"] = str(tmp_path / "papers" / f".staging_{tx_id}")
    j["final_path"] = str(tmp_path / "papers" / "2024_Smith_test")

    journal_path = _make_commit_journal(tmp_path / "transactions", tx_id, j)

    with pytest.raises(Exception) as exc:
        validate_commit_journal(
            journal=j,
            journal_path=journal_path,
            paper_raw_root=paper_raw_root,
            papers_root=tmp_path / "papers",
            transaction_root=tmp_path / "transactions",
        )
    msg = str(exc.value).lower()
    assert "expected_name" in msg or "basename" in msg or "mismatch" in msg


# ── Sentinel: rollback raw_path points to wrong paper number ────────────


def test_rollback_rejects_wrong_raw_path(tmp_path: Path) -> None:
    """If a rollback journal's ``raw_path`` basename does not match
    ``paper_number``, the validator raises an identity error."""
    paper_raw_root = tmp_path / "paper_raw"
    papers_root = tmp_path / "papers"
    tx_root = tmp_path / "transactions"
    pnum = "0000000000000001"
    pid = "2024_Smith_test"
    (paper_raw_root / pnum).mkdir(parents=True)
    (papers_root / pid).mkdir(parents=True)
    tx_id = str(uuid4())

    # raw_path points to paper_number "0000000000000099" — mismatch
    wrong_raw = paper_raw_root / "0000000000000099"
    wrong_raw.mkdir()

    j = _common(tx_id, pnum)
    j["phase"] = "quarantine"
    j["formal_path"] = str(papers_root / pid)
    j["raw_path"] = str(wrong_raw)
    j["staging_path"] = str(paper_raw_root / f".rollback_{pnum}_{tx_id}")
    j["formal_quarantine"] = str(papers_root / f".{pid}.rollback_{tx_id}")

    journal_path = _make_rollback_journal(tx_root, tx_id, j)

    with pytest.raises(Exception) as exc:
        validate_rollback_journal(
            journal=j,
            journal_path=journal_path,
            paper_raw_root=paper_raw_root,
            papers_root=papers_root,
            transaction_root=tx_root,
        )
    msg = str(exc.value).lower()
    assert "expected_name" in msg or "basename" in msg or "identity" in msg
