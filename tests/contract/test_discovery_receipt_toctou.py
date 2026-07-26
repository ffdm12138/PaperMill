"""Contract tests for discovery receipt TOCTOU safety (Phase 0.1).

Verifies that two concurrent writers to the same receipt path cannot:
- both succeed with ``created`` (double-create), or
- silently overwrite each other's receipt.

The tests use ``multiprocessing.Process`` (not threads) because ``filelock``
is a process-level lock. A ``Barrier`` synchronises the start so both
processes hit the writer at the same instant.
"""
from __future__ import annotations

import json
import multiprocessing
import sys
import traceback
from pathlib import Path

import pytest

from src.workspace.receipt import (
    DiscoveryReceiptConflictError,
    build_receipt_payload,
    receipt_path_for,
    write_or_validate_discovery_receipt,
)


pytestmark = pytest.mark.contract


def _worker_write_receipt(
    receipt_path_str: str,
    candidate_id: str,
    paper_number: str,
    barrier: multiprocessing.Barrier,
    result_queue: multiprocessing.Queue,
) -> None:
    """Worker that writes a receipt, reporting outcome via queue."""
    try:
        barrier.wait()  # release simultaneously with the other process
        payload = build_receipt_payload(
            candidate_id=candidate_id,
            page_id="p1",
            keyword_id="kw1",
            normalized_doi="10.1234/abc",
            paper_number=paper_number,
        )
        result = write_or_validate_discovery_receipt(
            Path(receipt_path_str), payload
        )
        result_queue.put(("ok", result.status, candidate_id))
    except DiscoveryReceiptConflictError:
        result_queue.put(("conflict", "", candidate_id))
    except Exception as exc:
        result_queue.put(("error", str(exc), candidate_id))


def _collect_results(result_queue: multiprocessing.Queue, expected: int, timeout: float = 30) -> list[tuple]:
    results = []
    for _ in range(expected):
        results.append(result_queue.get(timeout=timeout))
    return results


def _join_cleanly(procs: list[multiprocessing.Process], timeout: float = 30) -> None:
    for proc in procs:
        proc.join(timeout=timeout)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)
        assert not proc.is_alive()
        assert proc.exitcode == 0


def _worker_conflict_loop(
    root: str,
    candidate_id: str,
    barrier: multiprocessing.Barrier,
    result_queue: multiprocessing.Queue,
) -> None:
    try:
        for i in range(100):
            paper_number = f"{i:016d}"
            receipt_path = receipt_path_for(Path(root), paper_number)
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            barrier.wait(timeout=15)
            payload = build_receipt_payload(
                candidate_id=candidate_id,
                page_id="p1",
                keyword_id="kw1",
                normalized_doi="10.1234/abc",
                paper_number=paper_number,
            )
            try:
                result = write_or_validate_discovery_receipt(receipt_path, payload)
                result_queue.put((i, "ok", result.status, candidate_id))
            except DiscoveryReceiptConflictError:
                result_queue.put((i, "conflict", "", candidate_id))
    except Exception as exc:
        try:
            barrier.abort()
        except Exception:
            pass
        result_queue.put((-1, "error", str(exc), candidate_id))


def test_concurrent_receipt_writes_different_identity_no_double_create(
    tmp_path: Path,
):
    """Two processes writing DIFFERENT candidate_ids to the same path.

    BEFORE fix: both see path.exists()==False, both enter atomic_write_json
    (lock serialises), second overwrites first silently.
    AFTER fix: the second writer sees the existing receipt inside the lock
    and raises DiscoveryReceiptConflictError. Exactly ONE receipt survives.
    """
    paper_number = "0000000000000001"
    receipt_path = receipt_path_for(tmp_path, paper_number)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)

    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    result_queue: multiprocessing.Queue = ctx.Queue()

    procs = [
        ctx.Process(
            target=_worker_write_receipt,
            args=(str(receipt_path), "candidate-a", paper_number, barrier, result_queue),
        ),
        ctx.Process(
            target=_worker_write_receipt,
            args=(str(receipt_path), "candidate-b", paper_number, barrier, result_queue),
        ),
    ]
    for p in procs:
        p.start()
    results = _collect_results(result_queue, expected=2)
    _join_cleanly(procs)

    # Exactly one "ok" with status "created", and one "conflict".
    ok_results = [r for r in results if r[0] == "ok"]
    conflict_results = [r for r in results if r[0] == "conflict"]
    error_results = [r for r in results if r[0] == "error"]

    assert len(error_results) == 0, f"unexpected errors: {error_results}"
    assert len(ok_results) == 1, (
        f"expected exactly one 'created', got {len(ok_results)}: {results}"
    )
    assert len(conflict_results) == 1, (
        f"expected exactly one 'conflict', got {len(conflict_results)}: {results}"
    )

    # The disk content must match the winner's identity.
    winner_id = ok_results[0][2]
    data = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert data["candidate_id"] == winner_id


def test_concurrent_receipt_writes_same_identity_one_created_one_match(
    tmp_path: Path,
):
    """Two processes writing the SAME identity: one created, one existing_match."""
    paper_number = "0000000000000001"
    receipt_path = receipt_path_for(tmp_path, paper_number)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)

    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    result_queue: multiprocessing.Queue = ctx.Queue()

    procs = [
        ctx.Process(
            target=_worker_write_receipt,
            args=(str(receipt_path), "candidate-a", paper_number, barrier, result_queue),
        ),
        ctx.Process(
            target=_worker_write_receipt,
            args=(str(receipt_path), "candidate-a", paper_number, barrier, result_queue),
        ),
    ]
    for p in procs:
        p.start()
    results = _collect_results(result_queue, expected=2)
    _join_cleanly(procs)

    error_results = [r for r in results if r[0] == "error"]
    assert len(error_results) == 0, f"unexpected errors: {error_results}"

    statuses = [r[1] for r in results if r[0] == "ok"]
    # One should be "created", the other "existing_match".
    assert "created" in statuses, f"expected a 'created', got: {results}"
    # The other should be "existing_match" (no double-create).
    created_count = statuses.count("created")
    assert created_count == 1, f"expected exactly one 'created', got {created_count}: {results}"


@pytest.mark.slow
def test_concurrent_conflict_loop_100_iterations(tmp_path: Path):
    """Run the conflict test 100 times to confirm no intermittent double-create."""
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    result_queue: multiprocessing.Queue = ctx.Queue()
    procs = [
        ctx.Process(target=_worker_conflict_loop, args=(str(tmp_path), "candidate-a", barrier, result_queue)),
        ctx.Process(target=_worker_conflict_loop, args=(str(tmp_path), "candidate-b", barrier, result_queue)),
    ]
    for proc in procs:
        proc.start()
    results = _collect_results(result_queue, expected=200, timeout=60)
    _join_cleanly(procs, timeout=30)

    by_iteration: dict[int, list[tuple]] = {}
    for result in results:
        assert result[0] >= 0, f"worker error: {result}"
        by_iteration.setdefault(result[0], []).append(result)
    assert set(by_iteration) == set(range(100))
    for i, iteration_results in sorted(by_iteration.items()):
        ok_count = sum(1 for r in iteration_results if r[1] == "ok")
        conflict_count = sum(1 for r in iteration_results if r[1] == "conflict")
        assert ok_count == 1 and conflict_count == 1, (
            f"iteration {i}: expected one ok and one conflict, got {iteration_results}"
        )
