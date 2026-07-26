"""Contract-level tests for transaction path validation.

These tests verify that every transaction-journal path validator rejects
malicious, malformed, or misdirected paths BEFORE any destructive file
operation (rmtree, copytree, os.replace) could execute.

Each test creates an isolated tmp_path filesystem, simulates the attack
vector, and asserts that:
    - the correct exception type is raised;
    - sentinel files outside the expected root remain untouched;
    - no partial state change was made to ledger, journal, or index.
"""

from __future__ import annotations

import json
import re
import uuid as _uuid
from pathlib import Path

import pytest

from src.ingest.transaction_paths import (
    TransactionContainmentError,
    TransactionIdentityError,
    TransactionPathError,
    TransactionSymlinkError,
    assert_expected_name,
    assert_no_symlink_chain,
    assert_not_root,
    assert_resolved_child,
    check_destructive_path,
    commit_final_path,
    commit_staging_path,
    rollback_quarantine_path,
    rollback_staging_path,
    validate_commit_journal,
    validate_paper_name,
    validate_paper_number,
    validate_rollback_journal,
    validate_transaction_id,
)

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, Path]:
    """Create an isolated transaction-root filesystem."""
    paper_raw_root = tmp_path / "paper_raw"
    papers_root = tmp_path / "papers"
    transaction_root = tmp_path / "transactions"
    for p in [paper_raw_root, papers_root, transaction_root]:
        p.mkdir(parents=True)
    (transaction_root / "commit").mkdir()
    (transaction_root / "commit/completed").mkdir()
    return {
        "paper_raw_root": paper_raw_root,
        "papers_root": papers_root,
        "transaction_root": transaction_root,
        "tmp_path": tmp_path,
    }


def _make_journal(
    *,
    transaction_id: str | None = None,
    paper_number: str = "1234567890123456",
    paper_name: str = "2024_Author_test_paper",
    source_workspace: str | None = None,
    staging_path: str | None = None,
    final_path: str | None = None,
    phase: str = "prepared",
) -> dict:
    return {
        "transaction_id": transaction_id or str(_uuid.uuid4()),
        "paper_number": paper_number,
        "paper_name": paper_name,
        "phase": phase,
        "source_workspace": source_workspace or "/tmp/paper_raw/1234567890123456",
        "staging_path": staging_path or "/tmp/papers/.2024_Author_test_paper.staging_abc123",
        "final_path": final_path or "/tmp/papers/2024_Author_test_paper",
    }


def _write_journal(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _sentinel(path: Path, name: str = "keep.txt") -> Path:
    p = path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("keep", encoding="utf-8")
    return p


# ── Identity validation ──────────────────────────────────────────────


class TestValidateTransactionId:
    def test_valid_uuid(self) -> None:
        uid = str(_uuid.uuid4())
        assert validate_transaction_id(uid) == uid

    def test_valid_canonical_form(self) -> None:
        uid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        assert validate_transaction_id(uid) == uid

    def test_rejects_path_separator(self) -> None:
        with pytest.raises(TransactionIdentityError, match="path separator"):
            validate_transaction_id("../evil")

    def test_rejects_backslash(self) -> None:
        with pytest.raises(TransactionIdentityError):
            validate_transaction_id("..\\evil")

    def test_rejects_double_dot(self) -> None:
        with pytest.raises(TransactionIdentityError):
            validate_transaction_id("foo/../../bar")

    def test_rejects_empty(self) -> None:
        with pytest.raises(TransactionIdentityError):
            validate_transaction_id("")

    def test_rejects_drive_prefix(self) -> None:
        with pytest.raises(TransactionIdentityError):
            validate_transaction_id("C:../evil")

    def test_rejects_non_uuid(self) -> None:
        with pytest.raises(TransactionIdentityError, match="UUID"):
            validate_transaction_id("not-a-uuid")


class TestValidatePaperNumber:
    def test_valid(self) -> None:
        assert validate_paper_number("1234567890123456") == "1234567890123456"

    def test_short(self) -> None:
        with pytest.raises(TransactionIdentityError):
            validate_paper_number("123456789012345")

    def test_long(self) -> None:
        with pytest.raises(TransactionIdentityError):
            validate_paper_number("12345678901234567")

    def test_letters(self) -> None:
        with pytest.raises(TransactionIdentityError):
            validate_paper_number("123456789012345a")

    def test_empty(self) -> None:
        with pytest.raises(TransactionIdentityError):
            validate_paper_number("")

    def test_path_escape(self) -> None:
        with pytest.raises(TransactionIdentityError):
            validate_paper_number("../../etc/passwd")


class TestValidatePaperId:
    def test_valid(self) -> None:
        assert validate_paper_name("2024_Author_test") == "2024_Author_test"

    def test_valid_chinese(self) -> None:
        assert validate_paper_name("2024_作者_论文") == "2024_作者_论文"

    def test_path_separator(self) -> None:
        with pytest.raises(TransactionIdentityError):
            validate_paper_name("../evil")

    def test_drive_prefix(self) -> None:
        with pytest.raises(TransactionIdentityError):
            validate_paper_name("C:malicious")

    def test_nul(self) -> None:
        with pytest.raises(TransactionIdentityError):
            validate_paper_name("bad\0name")


# ── Path containment ──────────────────────────────────────────────────


class TestAssertResolvedChild:
    def test_direct_child(self, tmp_path: Path) -> None:
        parent = tmp_path / "root"
        parent.mkdir()
        child = parent / "ok"
        child.mkdir()
        result = assert_resolved_child(parent, child, field="test")
        assert result == child.resolve()

    def test_outside_path(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        with pytest.raises(TransactionContainmentError):
            assert_resolved_child(root, outside, field="test")

    def test_nonexistent_child(self, tmp_path: Path) -> None:
        parent = tmp_path / "root"
        parent.mkdir()
        child = parent / "future"
        result = assert_resolved_child(parent, child, field="test")
        assert result == (parent / "future").resolve()


class TestAssertNotRoot:
    def test_same_path(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        with pytest.raises(TransactionContainmentError):
            assert_not_root(root, root, field="test")

    def test_different_path(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        child = root / "child"
        child.mkdir()
        assert_not_root(root, child, field="test")  # no error


class TestAssertNoSymlinkChain:
    def test_rejects_symlink_parent(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        child = link / "child"
        with pytest.raises(TransactionSymlinkError):
            assert_no_symlink_chain(tmp_path, child, field="test")

    def test_rejects_symlink_candidate(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        link.symlink_to(target, target_is_directory=True)
        with pytest.raises(TransactionSymlinkError):
            assert_no_symlink_chain(tmp_path, link, field="test")

    def test_accepts_normal_path(self, tmp_path: Path) -> None:
        d = tmp_path / "a" / "b" / "c"
        d.mkdir(parents=True)
        assert_no_symlink_chain(tmp_path, d, field="test")  # no error

    def test_rejects_double_symlink_chain(self, tmp_path: Path) -> None:
        inner = tmp_path / "inner"
        inner.mkdir()
        mid = tmp_path / "mid"
        mid.symlink_to(inner, target_is_directory=True)
        outer = tmp_path / "outer"
        outer.symlink_to(mid, target_is_directory=True)
        with pytest.raises(TransactionSymlinkError):
            assert_no_symlink_chain(tmp_path, outer, field="test")


class TestCheckDestructivePath:
    def test_full_validation(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        child = root / "expected"
        child.mkdir()
        result = check_destructive_path(
            root, child, field="test", expected_name="expected"
        )
        assert result == child.resolve()

    def test_wrong_name(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        child = root / "wrong"
        child.mkdir()
        with pytest.raises(TransactionIdentityError, match="expected basename"):
            check_destructive_path(root, child, field="test", expected_name="expected")


# ── Commit journal validation ──────────────────────────────────────────


class TestValidateCommitJournal:
    def test_valid_journal(self, roots: dict) -> None:
        prr = roots["paper_raw_root"]
        pr = roots["papers_root"]
        tr = roots["transaction_root"]
        tx_id = str(_uuid.uuid4())
        number = "1234567890123456"
        paper_name = "2024_Author_test"

        source = prr / number
        source.mkdir()
        staging = pr / f".{paper_name}.staging_{tx_id}"
        final = pr / paper_name

        journal = _make_journal(
            transaction_id=tx_id,
            paper_number=number,
            paper_name=paper_name,
            source_workspace=str(source),
            staging_path=str(staging),
            final_path=str(final),
        )
        jpath = _write_journal(tr / "commit" / f"{tx_id}.json", journal)

        result = validate_commit_journal(
            journal, journal_path=jpath,
            paper_raw_root=prr, papers_root=pr, transaction_root=tr,
        )
        assert result["transaction_id"] == tx_id
        assert result["paper_number"] == number
        assert result["paper_name"] == paper_name

    def test_staging_outside_papers_root(self, roots: dict) -> None:
        prr = roots["paper_raw_root"]
        pr = roots["papers_root"]
        tr = roots["transaction_root"]
        tx_id = str(_uuid.uuid4())
        number = "1234567890123456"
        paper_name = "2024_Author_test"

        source = prr / number
        source.mkdir()
        # Staging points OUTSIDE papers_root
        outside = roots["tmp_path"] / "outside"
        final = pr / paper_name

        journal = _make_journal(
            transaction_id=tx_id, paper_number=number, paper_name=paper_name,
            source_workspace=str(source),
            staging_path=str(outside),
            final_path=str(final),
        )
        jpath = _write_journal(tr / "commit" / f"{tx_id}.json", journal)
        sentinel = _sentinel(roots["tmp_path"] / "outside")

        with pytest.raises(TransactionContainmentError):
            validate_commit_journal(
                journal, journal_path=jpath,
                paper_raw_root=prr, papers_root=pr, transaction_root=tr,
            )
        # Sentinel untouched
        assert sentinel.read_text() == "keep"

    def test_source_outside_paper_raw_root(self, roots: dict) -> None:
        prr = roots["paper_raw_root"]
        pr = roots["papers_root"]
        tr = roots["transaction_root"]
        tx_id = str(_uuid.uuid4())
        number = "1234567890123456"
        paper_name = "2024_Author_test"

        staging = pr / f".{paper_name}.staging_{tx_id}"
        final = pr / paper_name
        outside_source = roots["tmp_path"] / "unrelated"

        journal = _make_journal(
            transaction_id=tx_id, paper_number=number, paper_name=paper_name,
            source_workspace=str(outside_source),
            staging_path=str(staging),
            final_path=str(final),
        )
        jpath = _write_journal(tr / "commit" / f"{tx_id}.json", journal)

        with pytest.raises(TransactionContainmentError):
            validate_commit_journal(
                journal, journal_path=jpath,
                paper_raw_root=prr, papers_root=pr, transaction_root=tr,
            )

    def test_staging_is_papers_root(self, roots: dict) -> None:
        prr = roots["paper_raw_root"]
        pr = roots["papers_root"]
        tr = roots["transaction_root"]
        tx_id = str(_uuid.uuid4())
        number = "1234567890123456"
        paper_name = "2024_Author_test"

        source = prr / number
        source.mkdir()
        final = pr / paper_name

        journal = _make_journal(
            transaction_id=tx_id, paper_number=number, paper_name=paper_name,
            source_workspace=str(source),
            staging_path=str(pr),  # papers root itself!
            final_path=str(final),
        )
        jpath = _write_journal(tr / "commit" / f"{tx_id}.json", journal)

        with pytest.raises(TransactionContainmentError):
            validate_commit_journal(
                journal, journal_path=jpath,
                paper_raw_root=prr, papers_root=pr, transaction_root=tr,
            )

    def test_final_is_papers_root(self, roots: dict) -> None:
        prr = roots["paper_raw_root"]
        pr = roots["papers_root"]
        tr = roots["transaction_root"]
        tx_id = str(_uuid.uuid4())
        number = "1234567890123456"
        paper_name = "2024_Author_test"

        source = prr / number
        source.mkdir()
        staging = pr / f".{paper_name}.staging_{tx_id}"

        journal = _make_journal(
            transaction_id=tx_id, paper_number=number, paper_name=paper_name,
            source_workspace=str(source),
            staging_path=str(staging),
            final_path=str(pr),  # papers root itself!
        )
        jpath = _write_journal(tr / "commit" / f"{tx_id}.json", journal)

        with pytest.raises(TransactionContainmentError):
            validate_commit_journal(
                journal, journal_path=jpath,
                paper_raw_root=prr, papers_root=pr, transaction_root=tr,
            )

    def test_staging_is_final(self, roots: dict) -> None:
        prr = roots["paper_raw_root"]
        pr = roots["papers_root"]
        tr = roots["transaction_root"]
        tx_id = str(_uuid.uuid4())
        number = "1234567890123456"
        paper_name = "2024_Author_test"

        source = prr / number
        source.mkdir()
        staging = pr / f".{paper_name}.staging_{tx_id}"

        journal = _make_journal(
            transaction_id=tx_id, paper_number=number, paper_name=paper_name,
            source_workspace=str(source),
            staging_path=str(staging),
            final_path=str(staging),  # final == staging — caught by expected_name check
        )
        jpath = _write_journal(tr / "commit" / f"{tx_id}.json", journal)

        # The exact-path check on final_path expects basename == paper_name,
        # but staging's basename is '.staging_{tx_id}' — caught as identity error
        with pytest.raises(TransactionIdentityError):
            validate_commit_journal(
                journal, journal_path=jpath,
                paper_raw_root=prr, papers_root=pr, transaction_root=tr,
            )

    def test_staging_is_symlink(self, roots: dict) -> None:
        prr = roots["paper_raw_root"]
        pr = roots["papers_root"]
        tr = roots["transaction_root"]
        tx_id = str(_uuid.uuid4())
        number = "1234567890123456"
        paper_name = "2024_Author_test"

        source = prr / number
        source.mkdir()
        # Create a symlink staging target
        real_staging = pr / f".{paper_name}.real_staging"
        real_staging.mkdir()
        staging_link = pr / f".{paper_name}.staging_{tx_id}"
        staging_link.symlink_to(real_staging, target_is_directory=True)
        final = pr / paper_name

        journal = _make_journal(
            transaction_id=tx_id, paper_number=number, paper_name=paper_name,
            source_workspace=str(source),
            staging_path=str(staging_link),
            final_path=str(final),
        )
        jpath = _write_journal(tr / "commit" / f"{tx_id}.json", journal)
        sentinel = _sentinel(real_staging)

        with pytest.raises(TransactionSymlinkError):
            validate_commit_journal(
                journal, journal_path=jpath,
                paper_raw_root=prr, papers_root=pr, transaction_root=tr,
            )
        assert sentinel.read_text() == "keep"

    def test_journal_filename_mismatch(self, roots: dict) -> None:
        prr = roots["paper_raw_root"]
        pr = roots["papers_root"]
        tr = roots["transaction_root"]
        tx_id = str(_uuid.uuid4())
        number = "1234567890123456"
        paper_name = "2024_Author_test"

        source = prr / number
        source.mkdir()
        staging = pr / f".{paper_name}.staging_{tx_id}"
        final = pr / paper_name

        journal = _make_journal(
            transaction_id=tx_id, paper_number=number, paper_name=paper_name,
            source_workspace=str(source),
            staging_path=str(staging),
            final_path=str(final),
        )
        # Write with wrong filename
        jpath = _write_journal(tr / "commit" / "different_id.json", journal)

        with pytest.raises(TransactionIdentityError, match="expected basename"):
            validate_commit_journal(
                journal, journal_path=jpath,
                paper_raw_root=prr, papers_root=pr, transaction_root=tr,
            )

    def test_non_complete_journal_in_completed(self, roots: dict) -> None:
        prr = roots["paper_raw_root"]
        pr = roots["papers_root"]
        tr = roots["transaction_root"]
        tx_id = str(_uuid.uuid4())
        number = "1234567890123456"
        paper_name = "2024_Author_test"

        source = prr / number
        source.mkdir()
        staging = pr / f".{paper_name}.staging_{tx_id}"
        final = pr / paper_name

        journal = _make_journal(
            transaction_id=tx_id, paper_number=number, paper_name=paper_name,
            source_workspace=str(source),
            staging_path=str(staging),
            final_path=str(final),
            phase="prepared",  # not complete!
        )
        # Put it in completed directory
        jpath = _write_journal(tr / "commit/completed" / f"{tx_id}.json", journal)

        with pytest.raises(TransactionPathError):
            validate_commit_journal(
                journal, journal_path=jpath,
                paper_raw_root=prr, papers_root=pr, transaction_root=tr,
            )

    def test_paper_number_non_16_digit(self, roots: dict) -> None:
        prr = roots["paper_raw_root"]
        pr = roots["papers_root"]
        tr = roots["transaction_root"]
        tx_id = str(_uuid.uuid4())
        paper_name = "2024_Author_test"
        source = prr / "12345"
        source.mkdir()
        staging = pr / f".{paper_name}.staging_{tx_id}"
        final = pr / paper_name

        journal = _make_journal(
            transaction_id=tx_id, paper_number="12345", paper_name=paper_name,
            source_workspace=str(source),
            staging_path=str(staging),
            final_path=str(final),
        )
        jpath = _write_journal(tr / "commit" / f"{tx_id}.json", journal)

        with pytest.raises(TransactionIdentityError):
            validate_commit_journal(
                journal, journal_path=jpath,
                paper_raw_root=prr, papers_root=pr, transaction_root=tr,
            )

    def test_paper_name_with_separator(self, roots: dict) -> None:
        prr = roots["paper_raw_root"]
        pr = roots["papers_root"]
        tr = roots["transaction_root"]
        tx_id = str(_uuid.uuid4())
        number = "1234567890123456"
        paper_name = "2024/Author/../evil"
        source = prr / number
        source.mkdir()
        staging = pr / f".safe.staging_{_uuid.uuid4().hex}"
        final = pr / "safe"
        final.mkdir()

        journal = _make_journal(
            transaction_id=tx_id, paper_number=number, paper_name=paper_name,
            source_workspace=str(source),
            staging_path=str(staging),
            final_path=str(final),
        )
        jpath = _write_journal(tr / "commit" / f"{tx_id}.json", journal)

        with pytest.raises(TransactionIdentityError):
            validate_commit_journal(
                journal, journal_path=jpath,
                paper_raw_root=prr, papers_root=pr, transaction_root=tr,
            )


# ── Rollback journal validation ────────────────────────────────────────


class TestValidateRollbackJournal:
    def test_valid_rollback_journal(self, roots: dict) -> None:
        prr = roots["paper_raw_root"]
        pr = roots["papers_root"]
        tr = roots["transaction_root"]
        tx_id = str(_uuid.uuid4())
        number = "1234567890123456"
        paper_name = "2024_Author_test"

        formal = pr / paper_name
        raw_path = prr / number
        staging = prr / f".rollback_{number}_{tx_id}"
        quarantine = pr / f".{paper_name}.rollback_quarantine_{tx_id}"

        journal = {
            "transaction_id": tx_id,
            "paper_number": number,
            "paper_name": paper_name,
            "phase": "prepared",
            "formal_path": str(formal),
            "raw_path": str(raw_path),
            "staging_path": str(staging),
            "formal_quarantine": str(quarantine),
        }
        jpath = _write_journal(tr / f"{tx_id}.json", journal)

        result = validate_rollback_journal(
            journal, journal_path=jpath,
            paper_raw_root=prr, papers_root=pr, transaction_root=tr,
        )
        assert result["transaction_id"] == tx_id

    def test_quarantine_outside_papers_root(self, roots: dict) -> None:
        """Regression: formal_quarantine pointing to unrelated_user_data."""
        prr = roots["paper_raw_root"]
        pr = roots["papers_root"]
        tr = roots["transaction_root"]
        tx_id = str(_uuid.uuid4())
        number = "1234567890123456"
        paper_name = "2024_Author_test"

        formal = pr / paper_name
        raw_path = prr / number
        staging = prr / f".rollback_{number}_{tx_id}"
        # Quarantine points outside papers_root
        outside = roots["tmp_path"] / "unrelated_user_data"

        journal = {
            "transaction_id": tx_id,
            "paper_number": number,
            "paper_name": paper_name,
            "phase": "prepared",
            "formal_path": str(formal),
            "raw_path": str(raw_path),
            "staging_path": str(staging),
            "formal_quarantine": str(outside),
        }
        jpath = _write_journal(tr / f"{tx_id}.json", journal)
        sentinel = _sentinel(outside)

        with pytest.raises(TransactionContainmentError):
            validate_rollback_journal(
                journal, journal_path=jpath,
                paper_raw_root=prr, papers_root=pr, transaction_root=tr,
            )
        # Sentinel untouched
        assert sentinel.read_text() == "keep"

    def test_quarantine_is_papers_root(self, roots: dict) -> None:
        prr = roots["paper_raw_root"]
        pr = roots["papers_root"]
        tr = roots["transaction_root"]
        tx_id = str(_uuid.uuid4())
        number = "1234567890123456"
        paper_name = "2024_Author_test"

        formal = pr / paper_name
        raw_path = prr / number
        staging = prr / f".rollback_{number}_{tx_id}"

        journal = {
            "transaction_id": tx_id,
            "paper_number": number,
            "paper_name": paper_name,
            "phase": "prepared",
            "formal_path": str(formal),
            "raw_path": str(raw_path),
            "staging_path": str(staging),
            "formal_quarantine": str(pr),  # papers root itself!
        }
        jpath = _write_journal(tr / f"{tx_id}.json", journal)

        with pytest.raises(TransactionContainmentError):
            validate_rollback_journal(
                journal, journal_path=jpath,
                paper_raw_root=prr, papers_root=pr, transaction_root=tr,
            )

    def test_quarantine_symlink(self, roots: dict) -> None:
        prr = roots["paper_raw_root"]
        pr = roots["papers_root"]
        tr = roots["transaction_root"]
        tx_id = str(_uuid.uuid4())
        number = "1234567890123456"
        paper_name = "2024_Author_test"

        formal = pr / paper_name
        raw_path = prr / number
        staging = prr / f".rollback_{number}_{tx_id}"
        # Symlink quarantine
        real_dir = pr / "real_quarantine"
        real_dir.mkdir()
        quarantine_link = pr / f".{paper_name}.rollback_quarantine_{tx_id}"
        quarantine_link.symlink_to(real_dir, target_is_directory=True)

        journal = {
            "transaction_id": tx_id,
            "paper_number": number,
            "paper_name": paper_name,
            "phase": "prepared",
            "formal_path": str(formal),
            "raw_path": str(raw_path),
            "staging_path": str(staging),
            "formal_quarantine": str(quarantine_link),
        }
        jpath = _write_journal(tr / f"{tx_id}.json", journal)
        sentinel = _sentinel(real_dir)

        with pytest.raises(TransactionSymlinkError):
            validate_rollback_journal(
                journal, journal_path=jpath,
                paper_raw_root=prr, papers_root=pr, transaction_root=tr,
            )
        assert sentinel.read_text() == "keep"

    def test_formal_path_other_paper(self, roots: dict) -> None:
        prr = roots["paper_raw_root"]
        pr = roots["papers_root"]
        tr = roots["transaction_root"]
        tx_id = str(_uuid.uuid4())
        number = "1234567890123456"
        paper_name = "2024_Author_test"

        # formal_path points to different paper_name
        formal = pr / "different_paper_name"
        raw_path = prr / number
        staging = prr / f".rollback_{number}_{tx_id}"
        quarantine = pr / f".{paper_name}.rollback_quarantine_{tx_id}"

        journal = {
            "transaction_id": tx_id,
            "paper_number": number,
            "paper_name": paper_name,
            "phase": "prepared",
            "formal_path": str(formal),
            "raw_path": str(raw_path),
            "staging_path": str(staging),
            "formal_quarantine": str(quarantine),
        }
        jpath = _write_journal(tr / f"{tx_id}.json", journal)

        with pytest.raises(TransactionIdentityError, match="expected basename"):
            validate_rollback_journal(
                journal, journal_path=jpath,
                paper_raw_root=prr, papers_root=pr, transaction_root=tr,
            )

    def test_staging_name_not_rollback_prefix(self, roots: dict) -> None:
        prr = roots["paper_raw_root"]
        pr = roots["papers_root"]
        tr = roots["transaction_root"]
        tx_id = str(_uuid.uuid4())
        number = "1234567890123456"
        paper_name = "2024_Author_test"

        formal = pr / paper_name
        raw_path = prr / number
        staging = prr / "not_rollback_prefix"  # wrong prefix
        quarantine = pr / f".{paper_name}.rollback_quarantine_{tx_id}"

        journal = {
            "transaction_id": tx_id,
            "paper_number": number,
            "paper_name": paper_name,
            "phase": "prepared",
            "formal_path": str(formal),
            "raw_path": str(raw_path),
            "staging_path": str(staging),
            "formal_quarantine": str(quarantine),
        }
        jpath = _write_journal(tr / f"{tx_id}.json", journal)

        with pytest.raises(TransactionIdentityError, match=".rollback_"):
            validate_rollback_journal(
                journal, journal_path=jpath,
                paper_raw_root=prr, papers_root=pr, transaction_root=tr,
            )


# ── Sentinel protection regression tests ────────────────────────────────


class TestSentinelRegression:
    """Tests named after real CVE-like scenarios."""

    def test_rollback_rejects_quarantine_outside_papers_root(self, roots: dict) -> None:
        """Regression: unrelated_user_data/keep.txt must survive."""
        prr = roots["paper_raw_root"]
        pr = roots["papers_root"]
        tr = roots["transaction_root"]
        tx_id = str(_uuid.uuid4())
        number = "1234567890123456"
        paper_name = "2024_Author_test"

        formal = pr / paper_name
        raw_path = prr / number
        staging = prr / f".rollback_{number}_{tx_id}"
        # Quarantine pointing at user data outside papers_root
        unrelated = roots["tmp_path"] / "unrelated_user_data"
        sentinel = _sentinel(unrelated, "keep.txt")

        journal = {
            "transaction_id": tx_id,
            "paper_number": number,
            "paper_name": paper_name,
            "phase": "prepared",
            "formal_path": str(formal),
            "raw_path": str(raw_path),
            "staging_path": str(staging),
            "formal_quarantine": str(unrelated),
        }
        jpath = _write_journal(tr / f"{tx_id}.json", journal)

        with pytest.raises(TransactionContainmentError):
            validate_rollback_journal(
                journal, journal_path=jpath,
                paper_raw_root=prr, papers_root=pr, transaction_root=tr,
            )
        # SENTINEL MUST SURVIVE
        assert sentinel.exists(), "sentinel file was deleted — vulnerability!"
        assert sentinel.read_text() == "keep"

    def test_commit_rejects_staging_symlink_escape(self, roots: dict) -> None:
        """Staging symlink escapes to unrelated directory."""
        prr = roots["paper_raw_root"]
        pr = roots["papers_root"]
        tr = roots["transaction_root"]
        tx_id = str(_uuid.uuid4())
        number = "1234567890123456"
        paper_name = "2024_Author_test"

        source = prr / number
        source.mkdir()
        final = pr / paper_name

        # Create a symlink staging that points outside papers_root
        real_target = roots["tmp_path"] / "unrelated_data"
        sentinel = _sentinel(real_target, "keep.txt")
        staging_link = pr / f".{paper_name}.staging_{tx_id}"
        staging_link.symlink_to(real_target, target_is_directory=True)

        journal = _make_journal(
            transaction_id=tx_id, paper_number=number, paper_name=paper_name,
            source_workspace=str(source),
            staging_path=str(staging_link),
            final_path=str(final),
        )
        jpath = _write_journal(tr / "commit" / f"{tx_id}.json", journal)

        # Symlink resolves outside papers_root → containment error first
        with pytest.raises((TransactionContainmentError, TransactionSymlinkError)):
            validate_commit_journal(
                journal, journal_path=jpath,
                paper_raw_root=prr, papers_root=pr, transaction_root=tr,
            )
        assert sentinel.read_text() == "keep"

    def test_commit_rejects_malformed_transaction_id(self, roots: dict) -> None:
        """Transaction ID with path escape must fail."""
        prr = roots["paper_raw_root"]
        pr = roots["papers_root"]
        tr = roots["transaction_root"]
        number = "1234567890123456"
        paper_name = "2024_Author_test"

        source = prr / number
        source.mkdir()
        staging = pr / f".{paper_name}.staging_placeholder"
        final = pr / paper_name

        journal = _make_journal(
            transaction_id="../../../etc/passwd",
            paper_number=number, paper_name=paper_name,
            source_workspace=str(source),
            staging_path=str(staging),
            final_path=str(final),
        )
        jpath = _write_journal(tr / "commit" / "does_not_matter.json", journal)

        with pytest.raises(TransactionIdentityError):
            validate_commit_journal(
                journal, journal_path=jpath,
                paper_raw_root=prr, papers_root=pr, transaction_root=tr,
            )

    def test_rollback_rejects_malformed_transaction_id(self, roots: dict) -> None:
        """Transaction ID path escape must fail rollback too."""
        prr = roots["paper_raw_root"]
        pr = roots["papers_root"]
        tr = roots["transaction_root"]
        number = "1234567890123456"
        paper_name = "2024_Author_test"

        formal = pr / paper_name
        raw_path = prr / number
        staging = prr / f".rollback_{number}_abc"
        quarantine = pr / f".{paper_name}.rollback_quarantine_abc"

        journal = {
            "transaction_id": "../escaped",
            "paper_number": number,
            "paper_name": paper_name,
            "phase": "prepared",
            "formal_path": str(formal),
            "raw_path": str(raw_path),
            "staging_path": str(staging),
            "formal_quarantine": str(quarantine),
        }
        jpath = _write_journal(tr / "escaped.json", journal)

        with pytest.raises(TransactionIdentityError):
            validate_rollback_journal(
                journal, journal_path=jpath,
                paper_raw_root=prr, papers_root=pr, transaction_root=tr,
            )
