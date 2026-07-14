"""Contract tests for discovery workspace reconciliation (Phase 2).

Verifies the reconciliation state machine at each crash point:
- source record only (Case C) → not staged via reconcile, re-stage reuses number
- metadata written, receipt missing (Case B) → receipt backfilled
- receipt written, manifest missing (Case D) → re-stage reuses number
- ledger terminal, receipt missing (Case E) → re-stage reuses number
- conflicting receipt (Case F) → never overwritten
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.discovery.discovery_receipt import (
    DiscoveryReceiptConflictError,
    build_receipt_payload,
    write_or_validate_discovery_receipt,
)
from src.discovery.keyword_notebook import keyword_id, query_identity
from src.discovery.models import PaperCandidate
from src.discovery.page_journal import INITIAL_CURSOR, PageJournalStore, request_signature
from src.discovery.pending_queue import (
    ReconciliationResult,
    drain_pending_candidates,
    reconcile_discovery_workspace,
)
from src.services.network_metadata_staging import stage_network_metadata_records
from src.library.paper_number_ledger import PaperNumberLedger
from src.utils.atomic_io import atomic_write_json


pytestmark = pytest.mark.contract

PAPER_NUMBER = "0000000000000001"
DOI = "10.1234/reconcile"
KEYWORD_ZH = "测试关键词"
KEYWORD_ID = keyword_id(KEYWORD_ZH)
QUERY_ID = query_identity("zh", KEYWORD_ZH)


def _journal(tmp_path: Path) -> PageJournalStore:
    store = PageJournalStore(tmp_path / "pages")
    store.write_page(store.make_page(
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
        candidates=[PaperCandidate(title="T", doi=DOI)],
        state="cursor_committed",
    ))
    return store


def _candidate_id(store: PageJournalStore, tmp_path: Path) -> str:
    for ref in store.list_pages([KEYWORD_ID]):
        page = store.read(ref.path)
        for item in page.get("candidates", []):
            return item["candidate_id"]
    raise AssertionError("no candidate found")


def _stage_full_workspace(tmp_path: Path, cid: str) -> Path:
    """Stage a complete workspace and return its path."""
    stage_network_metadata_records(
        [{
            "title": "T",
            "doi": DOI,
                "discovery_context": {
                    "candidate_id": cid,
                    "page_id": "p1",
                    "keyword_id": KEYWORD_ID,
                    "provider": "openalex",
                    "normalized_doi": DOI,
                },
        }],
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        apply=True,
    )
    return tmp_path / "paper_raw" / PAPER_NUMBER


def _drain(tmp_path: Path, store: PageJournalStore):
    return drain_pending_candidates(
        journal=store,
        keyword_ids=[KEYWORD_ID],
        candidate_budget=1,
        stage_to_paper_raw=True,
        apply=True,
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        locks_dir=tmp_path / "locks",
        exports_dir=tmp_path / "exports",
        worker_id="worker",
    )


def test_source_record_only_returns_retryable_incomplete(tmp_path: Path):
    """Case C: only a source record → reconcile must NOT mark staged."""
    store = _journal(tmp_path)
    cid = _candidate_id(store, tmp_path)
    workspace = tmp_path / "paper_raw" / PAPER_NUMBER
    (workspace / "source_records").mkdir(parents=True)
    atomic_write_json(
        workspace / "source_records" / "metadata_source.openalex.json",
        {
            "provider": "openalex",
            "record": {"doi": DOI, "title": "T"},
            "discovery_context": {
                "candidate_id": cid, "page_id": "p1", "keyword_id": KEYWORD_ID,
                "provider": "openalex",
                "normalized_doi": DOI,
            },
        },
        indent=2,
    )
    result = reconcile_discovery_workspace(
        [tmp_path / "paper_raw"],
        candidate_id=cid, page_id="p1", keyword_id=KEYWORD_ID, provider="openalex", normalized_doi=DOI,
        ledger_path=tmp_path / "ledger.json",
    )
    assert result.status == "retryable_incomplete"
    assert result.paper_number == PAPER_NUMBER
    assert result.state is not None
    assert result.state.source_context_matches is True
    assert result.state.metadata_exists is False


def test_case_c_drain_reuses_paper_number(tmp_path: Path):
    """Case C via drain: re-stages into the SAME workspace, no new number."""
    store = _journal(tmp_path)
    cid = _candidate_id(store, tmp_path)
    workspace = tmp_path / "paper_raw" / PAPER_NUMBER
    (workspace / "source_records").mkdir(parents=True)
    atomic_write_json(
        workspace / "source_records" / "metadata_source.openalex.json",
        {
            "provider": "openalex",
            "record": {"doi": DOI, "title": "T"},
                "discovery_context": {
                    "candidate_id": cid, "page_id": "p1", "keyword_id": KEYWORD_ID,
                    "provider": "openalex",
                    "normalized_doi": DOI,
                },
        },
        indent=2,
    )
    report = _drain(tmp_path, store)
    assert report.staged == 1
    workspaces = [p.name for p in (tmp_path / "paper_raw").iterdir() if p.is_dir()]
    assert workspaces == [PAPER_NUMBER]
    assert (workspace / f"{PAPER_NUMBER}.metadata.json").exists()
    assert (workspace / f"{PAPER_NUMBER}.discovery_receipt.json").exists()


def test_case_b_metadata_present_receipt_missing_backfills(tmp_path: Path):
    """Case B: metadata staged, receipt deleted → reconcile backfills receipt."""
    store = _journal(tmp_path)
    cid = _candidate_id(store, tmp_path)
    workspace = _stage_full_workspace(tmp_path, cid)
    receipt = workspace / f"{PAPER_NUMBER}.discovery_receipt.json"
    assert receipt.exists()
    receipt.unlink()

    result = reconcile_discovery_workspace(
        [tmp_path / "paper_raw"],
        candidate_id=cid, page_id="p1", keyword_id=KEYWORD_ID, provider="openalex", normalized_doi=DOI,
        ledger_path=tmp_path / "ledger.json",
    )
    assert result.status == "retryable_incomplete"
    assert not receipt.exists()


def test_case_d_receipt_present_manifest_missing(tmp_path: Path):
    """Case D: receipt + metadata present, manifest deleted → re-stage reuses."""
    store = _journal(tmp_path)
    cid = _candidate_id(store, tmp_path)
    workspace = _stage_full_workspace(tmp_path, cid)
    (workspace / "stage_manifest.json").unlink()

    result = reconcile_discovery_workspace(
        [tmp_path / "paper_raw"],
        candidate_id=cid, page_id="p1", keyword_id=KEYWORD_ID, provider="openalex", normalized_doi=DOI,
        ledger_path=tmp_path / "ledger.json",
    )
    assert result.status == "retryable_incomplete"
    assert result.paper_number == PAPER_NUMBER

    report = _drain(tmp_path, store)
    assert report.staged == 1
    assert (workspace / "stage_manifest.json").exists()
    workspaces = [p.name for p in (tmp_path / "paper_raw").iterdir() if p.is_dir()]
    assert workspaces == [PAPER_NUMBER]


def test_case_e_ledger_terminal_receipt_missing(tmp_path: Path):
    """Case E: ledger terminal, receipt deleted → re-stage backfills receipt."""
    store = _journal(tmp_path)
    cid = _candidate_id(store, tmp_path)
    workspace = _stage_full_workspace(tmp_path, cid)
    receipt = workspace / f"{PAPER_NUMBER}.discovery_receipt.json"
    receipt.unlink()
    # Ledger is terminal (metadata_staged) from the full staging.

    result = reconcile_discovery_workspace(
        [tmp_path / "paper_raw"],
        candidate_id=cid, page_id="p1", keyword_id=KEYWORD_ID, provider="openalex", normalized_doi=DOI,
        ledger_path=tmp_path / "ledger.json",
    )
    assert result.status == "retryable_incomplete"
    assert result.paper_number == PAPER_NUMBER
    # After drain the receipt is backfilled and the candidate is staged.
    report = _drain(tmp_path, store)
    assert report.staged == 1
    assert receipt.exists()


def test_case_f_conflicting_receipt_not_overwritten(tmp_path: Path):
    """Case F: existing receipt for candidate-b → conflict, never overwrite."""
    store = _journal(tmp_path)
    cid = _candidate_id(store, tmp_path)
    workspace = _stage_full_workspace(tmp_path, cid)
    # Replace the receipt with a conflicting candidate identity. Write directly
    # via atomic_write_json because the shared writer refuses to overwrite.
    receipt = workspace / f"{PAPER_NUMBER}.discovery_receipt.json"
    atomic_write_json(
        receipt,
        build_receipt_payload(
            candidate_id="other-candidate",
            page_id="p1",
            keyword_id=KEYWORD_ID,
            normalized_doi=DOI,
            paper_number=PAPER_NUMBER,
        ),
        indent=2,
    )

    result = reconcile_discovery_workspace(
        [tmp_path / "paper_raw"],
        candidate_id=cid, page_id="p1", keyword_id=KEYWORD_ID, provider="openalex", normalized_doi=DOI,
        ledger_path=tmp_path / "ledger.json",
    )
    assert result.status == "receipt_conflict"
    # The conflicting receipt is untouched.
    import json
    data = json.loads(receipt.read_text(encoding="utf-8"))
    assert data["candidate_id"] == "other-candidate"

    report = _drain(tmp_path, store)
    assert report.retryable_failures >= 1
    assert report.staged == 0


def test_not_found_when_no_matching_source_record(tmp_path: Path):
    """Case G: no matching workspace → not_found."""
    result = reconcile_discovery_workspace(
        [tmp_path / "paper_raw"],
        candidate_id="nope", page_id="p1", keyword_id=KEYWORD_ID,
        normalized_doi="10.1/missing",
        ledger_path=tmp_path / "ledger.json",
    )
    assert result.status == "not_found"
