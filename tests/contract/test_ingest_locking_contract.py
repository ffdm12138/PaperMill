from pathlib import Path

import pytest

from src.ingest.locking import (
    INDEX_PUBLISH_RANK,
    TRANSACTION_RANK,
    WORKSPACE_RANK,
    LockRequest,
    acquire_locks,
    held_lock_ranks,
    transaction_requests,
)


def test_multi_paper_transaction_locks_are_numeric_and_unique(tmp_path: Path):
    numbers = ["0000000000000010", "0000000000000002"]
    requests = transaction_requests(tmp_path / "locks", numbers)
    assert [request.order_key[0] for request in requests] == [2, 10]
    assert all(request.rank == TRANSACTION_RANK for request in requests)
    assert all(request.path.parent == tmp_path / "locks" for request in requests)


def test_lock_rank_inversion_fails_before_filesystem_mutation(tmp_path: Path):
    high = LockRequest.path_lock(INDEX_PUBLISH_RANK, tmp_path / "index.lock")
    low = LockRequest.path_lock(WORKSPACE_RANK, tmp_path / "workspace.lock")
    with acquire_locks(high):
        assert held_lock_ranks() == (INDEX_PUBLISH_RANK,)
        with pytest.raises(RuntimeError, match="lock rank inversion"):
            with acquire_locks(low):
                pass
    assert not (tmp_path / "workspace.lock").exists()


def test_duplicate_transaction_identity_fails_closed(tmp_path: Path):
    number = "0000000000000001"
    with pytest.raises(ValueError, match="duplicate paper_number"):
        transaction_requests(tmp_path / "locks", [number, number])
