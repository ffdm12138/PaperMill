"""Ranked lock coordination for ingest, rollback, migration and indexing."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from filelock import FileLock


TRANSACTION_RANK = 1
PAPER_RAW_GLOBAL_RANK = 2
LEDGER_RANK = 3
WORKSPACE_RANK = 4
PAPERS_INSTALL_RANK = 5
INDEX_PUBLISH_RANK = 6


@dataclass(frozen=True, order=True)
class LockRequest:
    rank: int
    order_key: tuple[int, str]
    path: Path

    @classmethod
    def path_lock(cls, rank: int, path: Path) -> "LockRequest":
        return cls(rank=rank, order_key=(0, str(Path(path).resolve()).casefold()), path=Path(path))

    @classmethod
    def paper_lock(cls, rank: int, path: Path, paper_number: str) -> "LockRequest":
        if len(paper_number) != 16 or not paper_number.isdigit():
            raise ValueError(f"invalid paper_number lock identity: {paper_number}")
        return cls(rank=rank, order_key=(int(paper_number), str(Path(path).resolve()).casefold()), path=Path(path))


_HELD: ContextVar[tuple[LockRequest, ...]] = ContextVar("ingest_held_locks", default=())


def _validate_requests(requests: Sequence[LockRequest]) -> list[LockRequest]:
    ordered = sorted(requests, key=lambda request: (request.rank, request.order_key))
    if len({str(request.path.resolve()).casefold() for request in ordered}) != len(ordered):
        raise ValueError("duplicate lock path requested")
    held = _HELD.get()
    if held and ordered and max(request.rank for request in held) > ordered[0].rank:
        raise RuntimeError(
            f"lock rank inversion: holding rank {max(request.rank for request in held)} "
            f"before acquiring rank {ordered[0].rank}"
        )
    for left, right in zip(ordered, ordered[1:]):
        if left.rank == right.rank and left.order_key > right.order_key:
            raise RuntimeError("same-rank locks must be acquired in canonical order")
    return ordered


@contextmanager
def acquire_locks(*requests: LockRequest, timeout: float = -1) -> Iterator[None]:
    ordered = _validate_requests(requests)
    token = _HELD.set((*_HELD.get(), *ordered))
    try:
        with ExitStack() as stack:
            for request in ordered:
                request.path.parent.mkdir(parents=True, exist_ok=True)
                stack.enter_context(FileLock(str(request.path), timeout=timeout))
            yield
    finally:
        _HELD.reset(token)


@contextmanager
def paper_raw_write_lock(paper_raw_dir: Path | str, *, timeout: float = -1) -> Iterator[None]:
    """Acquire ``<paper_raw>/.paper_raw_write.lock`` at its canonical rank.

    The single sanctioned acquisition point for the workspace write lock
    (rank ``PAPER_RAW_GLOBAL_RANK``).  Modules must use this instead of
    constructing a raw ``FileLock`` so the ContextVar rank bookkeeping can
    fail fast on lock-order inversions.
    """
    lock_path = Path(paper_raw_dir) / ".paper_raw_write.lock"
    with acquire_locks(
        LockRequest.path_lock(PAPER_RAW_GLOBAL_RANK, lock_path), timeout=timeout
    ):
        yield


def transaction_requests(lock_root: Path, paper_numbers: Sequence[str]) -> list[LockRequest]:
    unique = sorted(set(paper_numbers), key=int)
    if len(unique) != len(paper_numbers):
        raise ValueError("duplicate paper_number transaction lock request")
    return [LockRequest.paper_lock(TRANSACTION_RANK, lock_root / f"{number}.lock", number) for number in unique]


def held_lock_ranks() -> tuple[int, ...]:
    """Expose ranks for deterministic tests and debug assertions."""
    return tuple(request.rank for request in _HELD.get())
