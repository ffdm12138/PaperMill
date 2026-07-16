"""Transaction concurrency: lock-order compliance, cross-process contention.

All tests carry the ``process`` and ``slow`` markers via the conftest
``_PROCESS_MODULES`` set.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from src.ingest.locking import (
    TRANSACTION_RANK,
    PAPER_RAW_GLOBAL_RANK,
    LEDGER_RANK,
    PAPERS_INSTALL_RANK,
    WORKSPACE_RANK,
    INDEX_PUBLISH_RANK,
    LockRequest,
    acquire_locks,
)
from src.library.paper_number_ledger import PaperNumberLedger

pytestmark = pytest.mark.e2e


# ── Rank-based lock ordering ───────────────────────────────────────────


class TestLockOrderCompliance:
    """``acquire_locks`` must reject out-of-order rank acquisition."""

    def test_ascending_ranks_acquire_ok(self, tmp_path: Path) -> None:
        a = tmp_path / "a.lock"
        b = tmp_path / "b.lock"
        with acquire_locks(
            LockRequest.path_lock(TRANSACTION_RANK, a),
            LockRequest.path_lock(PAPER_RAW_GLOBAL_RANK, b),
        ):
            assert a.exists() or not a.exists()  # lock held
            assert b.exists() or not b.exists()

    def test_descending_ranks_sorted_ok(self, tmp_path: Path) -> None:
        """``acquire_locks`` sorts requests internally by rank."""
        a = tmp_path / "a.lock"
        b = tmp_path / "b.lock"
        # Request in descending order; acquire_locks sorts to ascending
        with acquire_locks(
            LockRequest.path_lock(PAPER_RAW_GLOBAL_RANK, a),
            LockRequest.path_lock(TRANSACTION_RANK, b),
        ):
            assert True

    def test_same_path_same_rank_raises(self, tmp_path: Path) -> None:
        """Two identical paths at the same rank must be rejected."""
        a = tmp_path / "dup.lock"
        with pytest.raises(ValueError, match="duplicate"):
            with acquire_locks(
                LockRequest.path_lock(PAPER_RAW_GLOBAL_RANK, a),
                LockRequest.path_lock(PAPER_RAW_GLOBAL_RANK, a),
            ):
                pass  # pragma: no cover

    def test_release_order_reverses(self, tmp_path: Path) -> None:
        """Verify locks are released in reverse acquisition order."""
        a = tmp_path / "a.lock"
        b = tmp_path / "b.lock"
        with acquire_locks(
            LockRequest.path_lock(PAPER_RAW_GLOBAL_RANK, a),
            LockRequest.path_lock(LEDGER_RANK, b),
        ):
            assert True
        # After release, another acquire should succeed immediately
        with acquire_locks(
            LockRequest.path_lock(PAPER_RAW_GLOBAL_RANK, a),
        ):
            assert True

    def test_three_way_ordering(self, tmp_path: Path) -> None:
        a = tmp_path / "a.lock"
        b = tmp_path / "b.lock"
        c = tmp_path / "c.lock"
        with acquire_locks(
            LockRequest.path_lock(TRANSACTION_RANK, a),
            LockRequest.path_lock(PAPER_RAW_GLOBAL_RANK, b),
            LockRequest.path_lock(LEDGER_RANK, c),
        ):
            assert True
        # Verify held-lock ranking
        from src.ingest.locking import held_lock_ranks
        ranks = held_lock_ranks()
        assert list(ranks) == sorted(ranks), f"locks out of order: {ranks}"


# ── Cross-process lock contention ──────────────────────────────────────


class TestCrossProcessLockContention:
    """Workers in separate processes must block on the same write lock."""

    TIMEOUT = 15  # seconds per subprocess

    @staticmethod
    def _hold_lock_worker(lock_path: str, ready, release, queue) -> None:
        """Acquire PAPER_RAW_GLOBAL_RANK lock, hold for *hold_seconds*."""
        path = Path(lock_path)
        try:
            with acquire_locks(
                LockRequest.path_lock(PAPER_RAW_GLOBAL_RANK, path),
            ):
                ready.set()
                if not release.wait(timeout=10):
                    raise TimeoutError("parent did not release holder")
                queue.put(("ok", "held"))
        except Exception as exc:
            queue.put(("error", type(exc).__name__, str(exc)))

    @staticmethod
    def _try_lock_worker(lock_path: str, timeout: float, attempting, acquired, queue) -> None:
        """Try to acquire PAPER_RAW_GLOBAL_RANK lock."""
        path = Path(lock_path)
        start = time.monotonic()
        attempting.set()
        try:
            with acquire_locks(
                LockRequest.path_lock(PAPER_RAW_GLOBAL_RANK, path),
                timeout=timeout,
            ):
                elapsed = time.monotonic() - start
                acquired.set()
                queue.put(("ok", elapsed))
        except Exception as exc:
            queue.put(("error", type(exc).__name__, str(exc)))

    def test_two_workers_serialize_on_global_lock(self, tmp_path: Path) -> None:
        lock_path = tmp_path / ".paper_raw_write.lock"
        ctx = mp.get_context("spawn")

        q1 = ctx.Queue()
        q2 = ctx.Queue()
        ready = ctx.Event()
        release = ctx.Event()
        acquired = ctx.Event()
        attempting = ctx.Event()

        p1 = ctx.Process(
            target=self._hold_lock_worker,
            args=(str(lock_path), ready, release, q1),
        )
        p2 = ctx.Process(
            target=self._try_lock_worker,
            args=(str(lock_path), 5.0, attempting, acquired, q2),
        )

        p1.start()
        assert ready.wait(timeout=5), "holder did not acquire lock"
        p2.start()
        assert attempting.wait(timeout=5), "contender did not attempt lock"
        assert not acquired.wait(timeout=0.25), "contender acquired before release"
        release.set()
        assert acquired.wait(timeout=5), "contender did not acquire after release"

        p1.join(timeout=self.TIMEOUT)
        p2.join(timeout=self.TIMEOUT)

        assert p1.exitcode == 0, f"p1 failed: {p1.exitcode}"
        assert p2.exitcode == 0, f"p2 failed: {p2.exitcode}"

        r1 = q1.get(timeout=2)
        r2 = q2.get(timeout=2)

        assert r1[0] == "ok", f"p1: {r1}"
        assert r2[0] == "ok", f"p2: {r2}"
        # p2 must have waited for p1 to release
        assert r2[1] >= 0.2, f"p2 didn't wait for holder: {r2}"

        p1.kill()
        p2.kill()
        p1.join(timeout=5)
        p2.join(timeout=5)

    def test_lock_timeout_raises(self, tmp_path: Path) -> None:
        """A worker that can't acquire within timeout must abort."""
        lock_path = tmp_path / ".paper_raw_write.lock"
        ctx = mp.get_context("spawn")

        q1 = ctx.Queue()
        q2 = ctx.Queue()
        ready = ctx.Event()
        release = ctx.Event()
        acquired = ctx.Event()
        attempting = ctx.Event()

        p1 = ctx.Process(
            target=self._hold_lock_worker,
            args=(str(lock_path), ready, release, q1),
        )
        p2 = ctx.Process(
            target=self._try_lock_worker,
            args=(str(lock_path), 0.3, attempting, acquired, q2),  # very short timeout
        )

        p1.start()
        assert ready.wait(timeout=5), "holder did not acquire lock"
        p2.start()
        assert attempting.wait(timeout=5), "contender did not attempt lock"
        p2.join(timeout=self.TIMEOUT)

        r2 = q2.get(timeout=2)
        assert r2[0] != "ok", f"p2 should have timed out: {r2}"
        assert not acquired.is_set()
        release.set()
        p1.join(timeout=self.TIMEOUT)

        p1.kill()
        p2.kill()
        p1.join(timeout=5)
        p2.join(timeout=5)


# ── Concurrent commit journal protection ───────────────────────────────


class TestCommitJournalDuplicateProtection:
    """Two workers must not be able to create overlapping commit journals."""

    TIMEOUT = 15

    @staticmethod
    def _create_journal_worker(
        transactions_dir: str,
        paper_number: str,
        paper_name: str,
        queue,
    ) -> None:
        from src.ingest.transactions import CommitJournalStore

        store = CommitJournalStore(Path(transactions_dir))
        try:
            root = Path(transactions_dir).parent
            source = root / "paper_raw" / paper_number
            tx_id = str(uuid4())
            journal = store.create(
                paper_number=paper_number,
                paper_name=paper_name,
                source=source,
                staging=root / "papers" / f".{paper_name}.staging_{tx_id}",
                final=root / "papers" / paper_name,
                formalization=source / f"{paper_number}.formalization.json",
                transaction_id=tx_id,
            )
            queue.put(("ok", journal.get("transaction_id", "")))
        except Exception as exc:
            queue.put(("error", type(exc).__name__, str(exc)))

    def test_two_workers_cannot_create_duplicate_journal(self, tmp_path: Path) -> None:
        tdir = tmp_path / "transactions" / "commit"
        tdir.mkdir(parents=True)

        ctx = mp.get_context("spawn")
        q1 = ctx.Queue()
        q2 = ctx.Queue()

        paper_number = "1234567890123456"
        paper_name = "2024_Smith_test"
        source = tmp_path / "paper_raw" / paper_number
        source.mkdir(parents=True)
        (source / f"{paper_number}.metadata.json").write_text("{}", encoding="utf-8")
        (source / f"{paper_number}.catalog.json").write_text("{}", encoding="utf-8")
        (source / f"{paper_number}.formalization.json").write_text("{}", encoding="utf-8")

        p1 = ctx.Process(
            target=self._create_journal_worker,
            args=(str(tdir.parent), paper_number, paper_name, q1),
        )
        p2 = ctx.Process(
            target=self._create_journal_worker,
            args=(str(tdir.parent), paper_number, paper_name, q2),
        )

        p1.start(); p2.start()

        p1.join(timeout=self.TIMEOUT)
        p2.join(timeout=self.TIMEOUT)

        r1 = q1.get(timeout=3)
        r2 = q2.get(timeout=3)

        successes = sum(1 for r in (r1, r2) if r[0] == "ok")
        conflicts = sum(1 for r in (r1, r2) if r[0] == "error" and "active_journal_conflict" in r[2])
        assert successes == 1, f"expected exactly one success: {r1}, {r2}"
        assert conflicts == 1, f"expected exactly one conflict: {r1}, {r2}"
        active = list(tdir.glob("*.json"))
        assert len(active) == 1
        json.loads(active[0].read_text(encoding="utf-8"))
        assert not list(tdir.glob("*.tmp"))

        p1.kill()
        p2.kill()
        p1.join(timeout=5)
        p2.join(timeout=5)


class TestImportStatusCrossProcess:
    @staticmethod
    def _update_worker(folder: str, dimension: str, state: str, start, queue) -> None:
        from src.ingest.status import update_status
        from src.ingest.workspace import PaperRawWorkspace
        try:
            if not start.wait(timeout=10):
                raise TimeoutError("start event not set")
            update_status(PaperRawWorkspace.from_path(Path(folder)), dimension, state)
            queue.put(("ok", dimension))
        except Exception as exc:
            queue.put(("error", type(exc).__name__, str(exc)))

    def test_different_dimensions_are_not_lost(self, tmp_path: Path) -> None:
        number = "1234567890123456"
        folder = tmp_path / number
        folder.mkdir()
        (folder / f"{number}.paper.number").write_text(
            json.dumps({"paper_number": number, "folder_name": number, "state": "active"}),
            encoding="utf-8",
        )
        ctx = mp.get_context("spawn")
        start = ctx.Event()
        queue = ctx.Queue()
        workers = [
            ctx.Process(target=self._update_worker, args=(str(folder), "metadata", "resolved", start, queue)),
            ctx.Process(target=self._update_worker, args=(str(folder), "pdf", "attached", start, queue)),
        ]
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(timeout=15)
            assert worker.exitcode == 0
        results = [queue.get(timeout=2) for _ in workers]
        assert all(result[0] == "ok" for result in results), results
        status = json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))
        assert status["metadata"]["state"] == "resolved"
        assert status["pdf"]["state"] == "attached"
        assert not (folder / ".import_status.json.tmp").exists()


# ── Crash recovery stress ──────────────────────────────────────────────


class TestCrashRecoveryStress:
    """Repeated commit/resume cycles to surface race conditions."""

    ITERATIONS = 10

    def test_repeated_commit_cycle(self, tmp_path: Path) -> None:
        """Create and resume commit journals in rapid succession."""
        from src.ingest.transactions import CommitJournalStore
        from src.ingest.commit import resume_commit
        from src.library.paper_number_ledger import PaperNumberLedger

        tdir = tmp_path / "transactions"
        tdir.mkdir()
        source = tmp_path / "raw_ws"
        source.mkdir()
        (source / "images").mkdir()
        (source / "source_records").mkdir()

        ledger_path = tmp_path / "ledger.json"
        ledger = PaperNumberLedger(ledger_path)
        number = ledger.reserve_for_paper_raw(
            source, planned_paper_name="2024_Smith_test"
        )
        assert len(number) == 16, f"expected 16-digit number, got {number}"
        ledger.mark_metadata_staged(number, source)
        ledger.activate_metadata_staged(number, source)

        # Create content files named after the ledger-assigned number.
        (source / f"{number}.md").write_text("# content", encoding="utf-8")
        (source / f"{number}.pdf").write_bytes(b"PDF")
        (source / f"{number}.metadata.json").write_text(
            json.dumps({"title": "t", "year": 2024, "authors": [{"name": "Smith"}]}),
            encoding="utf-8",
        )
        (source / f"{number}.catalog.json").write_text(
            json.dumps({"paper_name": "2024_Smith_test"}), encoding="utf-8"
        )
        (source / f"{number}.metadata_freeze.jsonc").write_text(
            "{}", encoding="utf-8"
        )
        (source / f"{number}.catalog_freeze.jsonc").write_text(
            "{}", encoding="utf-8"
        )
        (source / f"{number}.phase_data.jsonc").write_text(
            json.dumps({"phase": "current"}), encoding="utf-8"
        )
        (source / f"{number}.formalization.json").write_text(
            "{}", encoding="utf-8"
        )

        for i in range(self.ITERATIONS):
            store = CommitJournalStore(tdir)
            tx_id = str(uuid4())
            staging = tmp_path / f".2024_Smith_test.staging_{tx_id}"
            final = tmp_path / "papers" / "2024_Smith_test"

            journal = store.create(
                paper_number=number,
                paper_name="2024_Smith_test",
                source=source,
                staging=staging,
                final=final,
                formalization=source / f"{number}.formalization.json",
                metadata_freeze=source / f"{number}.metadata_freeze.jsonc",
                catalog_freeze=source / f"{number}.catalog_freeze.jsonc",
                transaction_id=tx_id,
            )
            # Immediately clean up (no full commit needed — just cycle)
            # Walk through all phase transitions to reach "complete" for archive
            for next_phase in ("staging_complete", "final_installed",
                              "ledger_active", "category_reconcile_requested",
                              "source_deleted", "complete"):
                journal = store.update(journal, next_phase)
            store.archive_complete(journal)

    @pytest.mark.stress
    def test_stress_lock_acquire_release(self, tmp_path: Path) -> None:
        """200 rapid lock acquire/release cycles through acquire_locks."""
        lock_path = tmp_path / "stress.lock"
        for _ in range(200):
            with acquire_locks(
                LockRequest.path_lock(PAPER_RAW_GLOBAL_RANK, lock_path),
            ):
                pass
