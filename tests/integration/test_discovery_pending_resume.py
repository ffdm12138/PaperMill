from __future__ import annotations

from pathlib import Path

import pytest

from src.discovery.models import PaperCandidate
from src.discovery.page_journal import INITIAL_CURSOR, PageJournalStore, request_signature
from src.discovery.pending_queue import drain_pending_candidates, export_candidate_once, write_discovery_receipt
from src.utils.atomic_io import atomic_write_json


pytestmark = pytest.mark.integration


def _page(store: PageJournalStore, tmp_path: Path, doi: str = "10.1234/resume") -> Path:
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
        candidates=[PaperCandidate(title="T", doi=doi)],
        state="cursor_committed",
    )
    return store.write_page(page)


def test_staging_receipt_only_does_not_restore_staged(tmp_path: Path):
    """A receipt ALONE (without metadata/manifest/ledger) must NOT mark staged.

    BEFORE fix: the receipt-only fast path directly marked the candidate as
    staged based on receipt identity alone, skipping completeness checks.
    AFTER fix: the receipt locates the workspace, but the candidate is only
    marked staged when the workspace is actually complete. With only a
    receipt and no metadata, the candidate falls through to normal staging.
    """
    store = PageJournalStore(tmp_path / "pages")
    path = _page(store, tmp_path)
    page = store.read(path)
    record = page["candidates"][0]
    write_discovery_receipt(
        tmp_path / "paper_raw",
        paper_number="0000000000000001",
        candidate_id=record["candidate_id"],
        page_id=page["page_id"],
        keyword_id=page["keyword_id"],
        normalized_doi=record["candidate"]["doi"],
    )
    report = drain_pending_candidates(
        journal=store,
        keyword_ids=["kw"],
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
    # The candidate IS staged (via normal staging, not reconciliation).
    assert report.staged == 1
    item = store.read(path)["candidates"][0]
    assert item["status"] == "staged"
    # But NOT reconciled — a receipt alone is not proof of complete staging.
    assert item.get("reconciled") is not True


def test_source_record_reconciliation_restores_missing_receipt(tmp_path: Path):
    """Case B: metadata staged, receipt missing → reconciler backfills receipt.

    A source record alone must NOT be enough to mark staged (Phase 2 contract).
    Here the workspace also carries valid metadata + manifest + import_status +
    a terminal ledger entry, so reconciliation backfills the receipt and marks
    the candidate staged without re-allocating a paper number.
    """
    from src.services.network_metadata_staging import stage_network_metadata_records
    from src.services.v2_library import PaperNumberLedger

    store = PageJournalStore(tmp_path / "pages")
    path = _page(store, tmp_path, doi="10.1234/source-record")
    page = store.read(path)
    record = page["candidates"][0]
    cid = record["candidate_id"]

    # Fully stage the candidate first so metadata/manifest/import-status/ledger
    # all exist, then remove only the receipt to simulate a crash between the
    # metadata write and the receipt write.
    stage_network_metadata_records(
        [{
            "title": "T",
            "doi": "10.1234/source-record",
            "discovery_context": {
                "candidate_id": cid,
                "page_id": page["page_id"],
                "keyword_id": page["keyword_id"],
                "normalized_doi": "10.1234/source-record",
            },
        }],
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        apply=True,
    )
    workspace = tmp_path / "paper_raw" / "0000000000000001"
    receipt = workspace / "0000000000000001.discovery_receipt.json"
    assert receipt.exists()
    receipt.unlink()
    # Reset the journal candidate so the drain loop re-processes it.
    store.write_page(store.make_page(
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
        candidates=[PaperCandidate(title="T", doi="10.1234/source-record")],
        state="cursor_committed",
    ))

    report = drain_pending_candidates(
        journal=store,
        keyword_ids=["kw"],
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

    assert report.staged == 1
    assert receipt.exists()
    item = store.read(path)["candidates"][0]
    assert item["status"] == "staged"
    # Paper number reused — no second workspace created.
    assert item["staged_paper_number"] == "0000000000000001"
    workspaces = [p.name for p in (tmp_path / "paper_raw").iterdir() if p.is_dir()]
    assert workspaces == ["0000000000000001"]


def test_source_record_only_not_marked_staged(tmp_path: Path):
    """Case C: source record written, metadata missing → not staged via reconcile.

    The reconciler must NOT mark the candidate staged when only a source record
    exists. It returns retryable_incomplete so the drain loop re-stages into the
    SAME workspace (reusing the paper number) to rebuild metadata — never
    allocating a new paper number or creating a second workspace.
    """
    store = PageJournalStore(tmp_path / "pages")
    path = _page(store, tmp_path, doi="10.1234/source-only")
    page = store.read(path)
    record = page["candidates"][0]
    cid = record["candidate_id"]
    workspace = tmp_path / "paper_raw" / "0000000000000001"
    (workspace / "source_records").mkdir(parents=True)
    atomic_write_json(
        workspace / "source_records" / "metadata_source.openalex.json",
        {
            "provider": "openalex",
            "record": {"doi": "10.1234/source-only", "title": "T"},
            "discovery_context": {
                "candidate_id": cid,
                "page_id": page["page_id"],
                "keyword_id": page["keyword_id"],
                "normalized_doi": "10.1234/source-only",
            },
        },
        indent=2,
    )

    report = drain_pending_candidates(
        journal=store,
        keyword_ids=["kw"],
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

    # Re-staged into the SAME workspace (reused paper number).
    assert report.staged == 1
    item = store.read(path)["candidates"][0]
    assert item["status"] == "staged"
    assert item["staged_paper_number"] == "0000000000000001"
    assert item["terminal_reason"] == "recovered_via_reuse"
    # No second workspace was created.
    workspaces = [p.name for p in (tmp_path / "paper_raw").iterdir() if p.is_dir()]
    assert workspaces == ["0000000000000001"]
    # Metadata was rebuilt.
    assert (workspace / "0000000000000001.metadata.json").exists()
    assert (workspace / "0000000000000001.discovery_receipt.json").exists()


def test_export_manifest_reconciliation_is_idempotent(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    path = _page(store, tmp_path, doi="10.1234/export")
    record = store.read(path)["candidates"][0]
    first = export_candidate_once(tmp_path / "exports", record)
    second = export_candidate_once(tmp_path / "exports", record)
    assert first["export_id"] == second["export_id"]
    assert second["reconciled"] is True
    assert Path(first["export_path"]).read_text(encoding="utf-8").count("\n") == 1

    report = drain_pending_candidates(
        journal=store,
        keyword_ids=["kw"],
        candidate_budget=1,
        stage_to_paper_raw=False,
        apply=False,
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        locks_dir=tmp_path / "locks",
        exports_dir=tmp_path / "exports",
        worker_id="worker",
    )
    assert report.emitted == 1
    item = store.read(path)["candidates"][0]
    assert item["status"] == "emitted"
    assert item["reconciled"] is True
