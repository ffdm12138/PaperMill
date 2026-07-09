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

from src.discovery.discovery_receipt import (
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

    barrier = multiprocessing.Barrier(2)
    result_queue: multiprocessing.Queue = multiprocessing.Queue()

    procs = [
        multiprocessing.Process(
            target=_worker_write_receipt,
            args=(str(receipt_path), "candidate-a", paper_number, barrier, result_queue),
        ),
        multiprocessing.Process(
            target=_worker_write_receipt,
            args=(str(receipt_path), "candidate-b", paper_number, barrier, result_queue),
        ),
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)

    results = []
    while not result_queue.empty():
        results.append(result_queue.get_nowait())

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

    barrier = multiprocessing.Barrier(2)
    result_queue: multiprocessing.Queue = multiprocessing.Queue()

    procs = [
        multiprocessing.Process(
            target=_worker_write_receipt,
            args=(str(receipt_path), "candidate-a", paper_number, barrier, result_queue),
        ),
        multiprocessing.Process(
            target=_worker_write_receipt,
            args=(str(receipt_path), "candidate-a", paper_number, barrier, result_queue),
        ),
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)

    results = []
    while not result_queue.empty():
        results.append(result_queue.get_nowait())

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
    for i in range(100):
        paper_number = f"{i:016d}"
        receipt_path = receipt_path_for(tmp_path, paper_number)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)

        barrier = multiprocessing.Barrier(2)
        result_queue: multiprocessing.Queue = multiprocessing.Queue()

        procs = [
            multiprocessing.Process(
                target=_worker_write_receipt,
                args=(str(receipt_path), "candidate-a", paper_number, barrier, result_queue),
            ),
            multiprocessing.Process(
                target=_worker_write_receipt,
                args=(str(receipt_path), "candidate-b", paper_number, barrier, result_queue),
            ),
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)

        results = []
        while not result_queue.empty():
            results.append(result_queue.get_nowait())

        ok_count = sum(1 for r in results if r[0] == "ok")
        assert ok_count == 1, (
            f"iteration {i}: expected 1 'created', got {ok_count}: {results}"
        )
