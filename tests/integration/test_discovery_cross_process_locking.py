from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

import pytest

from src.discovery.keyword_notebook import keyword_id, query_identity
from src.discovery.models import PaperCandidate
from src.discovery.page_journal import INITIAL_CURSOR, PageJournalStore, request_signature
from src.services.network_metadata_staging import stage_network_metadata_records
from src.library.paper_number_ledger import PaperNumberLedger
from src.utils.atomic_io import atomic_write_json


pytestmark = [pytest.mark.integration, pytest.mark.process]


def _claim_worker(root: str, path: str, cid: str, worker: str, queue) -> None:
    store = PageJournalStore(Path(root))
    result = store.claim_candidate(Path(path), candidate_id_value=cid, worker_id=worker, lease_seconds=30, expected_profile_hash="test-hash")
    queue.put((worker, result.claimed, result.reason))


def _stage_worker(
    paper_raw: str,
    papers: str,
    ledger: str,
    record: dict,
    reuse_number: str,
    queue,
) -> None:
    try:
        report = stage_network_metadata_records(
            [record],
            paper_raw_dir=Path(paper_raw),
            papers_dir=Path(papers),
            ledger_path=Path(ledger),
            apply=True,
            reuse_paper_number=reuse_number or None,
        )
        queue.put(("ok", report["items"][0]["status"], report["items"][0].get("paper_number", "")))
    except Exception as exc:
        queue.put(("error", type(exc).__name__, str(exc)))


def test_candidate_claim_uses_real_os_filelock_across_processes(tmp_path: Path):
    ctx = mp.get_context("spawn")
    store = PageJournalStore(tmp_path / "pages")
    page = store.make_synthetic_page(
        page_id="p1",
        keyword_id=KEYWORD_ID,
        keyword_zh=KEYWORD_ZH,
        query_id=QUERY_ID,
        query=KEYWORD_ZH,
        query_language="zh",
        provider="openalex",
        lane="refresh",
        request_signature_value=request_signature(page_size=10),
        request_cursor=INITIAL_CURSOR,
        next_cursor=None,
        provider_exhausted=True,
        relevance_profile_hash="test-hash",
        candidates=[PaperCandidate(title="T", doi="10.1234/process")],
        state="cursor_committed",
    )
    page["candidates"][0]["relevance"]["state"] = "passed"
    page["candidates"][0]["relevance"]["reason"] = "profile_match"
    path = store.write_page(page)
    cid = store.read(path)["candidates"][0]["candidate_id"]

    queue = ctx.Queue()
    processes = [
        ctx.Process(target=_claim_worker, args=(str(store.root_dir), str(path), cid, "worker-a", queue)),
        ctx.Process(target=_claim_worker, args=(str(store.root_dir), str(path), cid, "worker-b", queue)),
    ]
    for proc in processes:
        proc.start()
    results = [queue.get(timeout=10), queue.get(timeout=10)]
    for proc in processes:
        proc.join(10)
        if proc.is_alive():
            proc.terminate()
            proc.join(5)
        assert not proc.is_alive()
        assert proc.exitcode == 0

    assert sum(1 for _, claimed, _ in results if claimed) == 1
    assert sum(1 for _, claimed, _ in results if not claimed) == 1


def test_allocator_new_allocation_and_reuse_do_not_deadlock_across_processes(tmp_path: Path):
    ctx = mp.get_context("spawn")
    paper_raw = tmp_path / "paper_raw"
    papers = tmp_path / "papers"
    ledger = tmp_path / "ledger.json"
    reuse_number = "0000000000000001"
    reuse_workspace = paper_raw / reuse_number
    (reuse_workspace / "source_records").mkdir(parents=True)
    PaperNumberLedger(ledger).reserve_specific_for_paper_raw(reuse_number, reuse_workspace)
    atomic_write_json(
        reuse_workspace / "source_records" / "metadata_source.openalex.json",
        {
            "provider": "openalex",
            "record": {"doi": "10.1234/reuse-lock", "title": "Reuse"},
            "discovery_context": {
                "candidate_id": "candidate-reuse",
                "page_id": "page-reuse",
                "keyword_id": "kw",
                "provider": "openalex",
                "normalized_doi": "10.1234/reuse-lock",
            },
        },
        indent=2,
    )
    reuse_record = {
        "title": "Reuse",
        "doi": "10.1234/reuse-lock",
        "discovery_context": {
            "candidate_id": "candidate-reuse",
            "page_id": "page-reuse",
            "keyword_id": "kw",
            "provider": "openalex",
            "normalized_doi": "10.1234/reuse-lock",
        },
    }
    new_record = {
        "title": "New",
        "doi": "10.1234/new-lock",
        "discovery_context": {
            "candidate_id": "candidate-new",
            "page_id": "page-new",
            "keyword_id": "kw",
            "provider": "openalex",
            "normalized_doi": "10.1234/new-lock",
        },
    }
    queue = ctx.Queue()
    processes = [
        ctx.Process(target=_stage_worker, args=(str(paper_raw), str(papers), str(ledger), reuse_record, reuse_number, queue)),
        ctx.Process(target=_stage_worker, args=(str(paper_raw), str(papers), str(ledger), new_record, "", queue)),
    ]
    for proc in processes:
        proc.start()
    results = [queue.get(timeout=20), queue.get(timeout=20)]
    for proc in processes:
        proc.join(20)
        if proc.is_alive():
            proc.terminate()
            proc.join(5)
        assert not proc.is_alive()
        assert proc.exitcode == 0

    assert sorted(result[1] for result in results) == ["staged", "staged"]
    assert sorted(p.name for p in paper_raw.iterdir() if p.is_dir()) == [
        "0000000000000001",
        "0000000000000002",
    ]
KEYWORD_ZH = "测试关键词"
KEYWORD_ID = keyword_id(KEYWORD_ZH)
QUERY_ID = query_identity("zh", KEYWORD_ZH)


@pytest.mark.parametrize("same_identity", [False, True])
def test_concurrent_same_doi_has_one_durable_primary(tmp_path: Path, same_identity: bool):
    ctx = mp.get_context("spawn")
    paper_raw, papers, ledger = tmp_path / "paper_raw", tmp_path / "papers", tmp_path / "ledger.json"
    base = {
        "title": "Concurrent", "doi": "10.1234/concurrent",
        "discovery_context": {
            "candidate_id": "candidate-a", "page_id": "page-a", "keyword_id": "kw",
            "provider": "openalex", "normalized_doi": "10.1234/concurrent",
        },
    }
    second = {**base, "discovery_context": dict(base["discovery_context"])}
    if not same_identity:
        second["discovery_context"].update(candidate_id="candidate-b", page_id="page-b")
    queue = ctx.Queue()
    processes = [
        ctx.Process(target=_stage_worker, args=(str(paper_raw), str(papers), str(ledger), record, "", queue))
        for record in (base, second)
    ]
    for process in processes:
        process.start()
    results = [queue.get(timeout=30), queue.get(timeout=30)]
    for process in processes:
        process.join(30)
        if process.is_alive():
            process.terminate(); process.join(5)
        assert process.exitcode == 0
    assert {result[2] for result in results} == {"0000000000000001"}, results
    assert sum(path.is_dir() for path in paper_raw.iterdir()) == 1
    assert PaperNumberLedger(ledger).load()["max_number"] == "0000000000000001"
    statuses = sorted(result[1] for result in results)
    assert statuses == (["staged", "staged"] if same_identity else ["duplicate", "staged"])
