from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

import pytest

from src.discovery.models import PaperCandidate
from src.discovery.page_journal import INITIAL_CURSOR, PageJournalStore, request_signature


pytestmark = pytest.mark.integration


def _claim_worker(root: str, path: str, cid: str, worker: str, queue) -> None:
    store = PageJournalStore(Path(root))
    result = store.claim_candidate(Path(path), candidate_id_value=cid, worker_id=worker, lease_seconds=30)
    queue.put((worker, result.claimed, result.reason))


def test_candidate_claim_uses_real_os_filelock_across_processes(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    page = store.make_page(
        page_id="p1",
        keyword_id="kw",
        keyword="kw",
        expansion_id="exp",
        expanded_query="kw",
        provider="openalex",
        lane="refresh",
        request_signature_value=request_signature(page_size=10),
        request_cursor=INITIAL_CURSOR,
        next_cursor=None,
        provider_exhausted=True,
        candidates=[PaperCandidate(title="T", doi="10.1234/process")],
        state="cursor_committed",
    )
    path = store.write_page(page)
    cid = store.read(path)["candidates"][0]["candidate_id"]

    queue = mp.Queue()
    processes = [
        mp.Process(target=_claim_worker, args=(str(store.root_dir), str(path), cid, "worker-a", queue)),
        mp.Process(target=_claim_worker, args=(str(store.root_dir), str(path), cid, "worker-b", queue)),
    ]
    for proc in processes:
        proc.start()
    for proc in processes:
        proc.join(10)
        assert proc.exitcode == 0

    results = [queue.get(timeout=5), queue.get(timeout=5)]
    assert sum(1 for _, claimed, _ in results if claimed) == 1
    assert sum(1 for _, claimed, _ in results if not claimed) == 1
