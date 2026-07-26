"""Post-cutover reconciliation + strict migration seed receipt tests.

Covers ``MigrationReceiptStoreV4`` strict seed receipts (create-if-absent,
idempotent evidence match, conflict/corruption errors) and the
``reconcile_migration`` verdict logic over synthetic legacy journals and
paper_raw workspaces.  All fixtures use ``tests.helpers.legacy_journals``
builders that mirror the real production journal schema.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.discovery.stores.migration_receipt_store import (
    MigrationReceiptConflictError,
    MigrationReceiptCorruptError,
    MigrationReceiptStoreV4,
    SEED_RECEIPT_SCHEMA_VERSION,
    validate_seed_receipt,
)
from src.migrations.discovery_v4.post_cutover_reconciliation import (
    recompute_importable_pool,
    reconcile_migration,
)
from tests.helpers.legacy_journals import make_candidate, make_journal


MIGRATION_ID = "v4-test-migration"
CREATED_AT = datetime(2026, 7, 25, 13, 32, 38, tzinfo=timezone.utc)
AFTER = "2026-07-25T14:35:00+00:00"
BEFORE = "2026-07-18T23:00:00+08:00"


def _receipt_record(seed_id: str, **overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": SEED_RECEIPT_SCHEMA_VERSION,
        "migration_id": MIGRATION_ID,
        "seed_id": seed_id,
        "candidate_id": "c" * 32,
        "normalized_doi": "10.5555/example",
        "outcome": "staged_new",
        "paper_number": "0000000000009001",
        "metadata_sha256": "a" * 64,
        "match_receipt_sha256": "",
        "ledger_entry_sha256": "b" * 64,
        "verified_at": "2026-07-25T15:00:00+00:00",
    }
    record.update(overrides)
    return record


class TestSeedReceiptStore:
    def test_requires_workspace_or_dir(self):
        with pytest.raises(ValueError):
            MigrationReceiptStoreV4()

    def test_write_and_read_roundtrip(self, tmp_path: Path):
        store = MigrationReceiptStoreV4(receipts_dir=tmp_path / "receipts")
        record = _receipt_record("s" * 32)
        path = store.write_seed_receipt(record)
        assert path.is_file()
        assert store.read_seed_receipt("s" * 32) == record
        assert store.count_seed_receipts() == 1

    def test_idempotent_rewrite_same_evidence_new_timestamp(self, tmp_path: Path):
        store = MigrationReceiptStoreV4(receipts_dir=tmp_path / "r")
        store.write_seed_receipt(_receipt_record("s" * 32))
        # Re-verification at a later time with identical evidence succeeds.
        store.write_seed_receipt(
            _receipt_record("s" * 32, verified_at="2026-07-26T00:00:00+00:00")
        )
        assert store.count_seed_receipts() == 1

    def test_conflicting_evidence_raises(self, tmp_path: Path):
        store = MigrationReceiptStoreV4(receipts_dir=tmp_path / "r")
        store.write_seed_receipt(_receipt_record("s" * 32))
        with pytest.raises(MigrationReceiptConflictError):
            store.write_seed_receipt(
                _receipt_record("s" * 32, paper_number="0000000000009999")
            )

    def test_read_missing_returns_none(self, tmp_path: Path):
        store = MigrationReceiptStoreV4(receipts_dir=tmp_path / "r")
        assert store.read_seed_receipt("nope") is None

    def test_read_corrupt_raises(self, tmp_path: Path):
        receipts = tmp_path / "r"
        receipts.mkdir()
        (receipts / f"{'s' * 32}.json").write_text("{not json", encoding="utf-8")
        store = MigrationReceiptStoreV4(receipts_dir=receipts)
        with pytest.raises(MigrationReceiptCorruptError):
            store.read_seed_receipt("s" * 32)

    def test_validate_rejects_bad_outcome(self):
        with pytest.raises(ValueError):
            validate_seed_receipt(_receipt_record("s" * 32, outcome="mystery"))

    def test_validate_rejects_missing_field(self):
        record = _receipt_record("s" * 32)
        del record["normalized_doi"]
        with pytest.raises(ValueError):
            validate_seed_receipt(record)


def _write_workspace(
    paper_raw: Path,
    paper_number: str,
    *,
    doi: str,
    receipt: dict[str, object] | None,
) -> Path:
    ws = paper_raw / paper_number
    ws.mkdir(parents=True)
    (ws / f"{paper_number}.metadata.json").write_text(
        json.dumps({
            "schema_version": "2.0",
            "paper_number": paper_number,
            "identifiers": {"doi": doi},
        }),
        encoding="utf-8",
    )
    (ws / "stage_manifest.json").write_text(
        json.dumps({"schema_version": "1.0", "paper_number": paper_number}),
        encoding="utf-8",
    )
    (ws / ".import_status.json").write_text(
        json.dumps({"schema_version": "2.0", "paper_number": paper_number,
                    "updated_at": BEFORE}),
        encoding="utf-8",
    )
    if receipt is not None:
        payload = {"schema_version": "1.0", "paper_number": paper_number}
        payload.update(receipt)
        (ws / f"{paper_number}.discovery_receipt.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
    return ws


def _write_journal(pages_dir: Path, name: str, candidates: list[dict]) -> None:
    lane = pages_dir / "kw" / "q" / "openalex" / "backfill"
    lane.mkdir(parents=True, exist_ok=True)
    (lane / name).write_text(
        json.dumps(make_journal(candidates)), encoding="utf-8"
    )


def _write_ledger(path: Path, entries: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"entries": entries}), encoding="utf-8")


def _run(tmp_path: Path, *, expected_imported: int, expected_seeds: int,
         write_receipts: bool = False):
    return reconcile_migration(
        migration_id=MIGRATION_ID,
        migration_created_at=CREATED_AT,
        expected_imported=expected_imported,
        expected_valid_doi_seeds=expected_seeds,
        legacy_pages_dir=tmp_path / "pending_pages",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger" / "paper_number_ledger.json",
        receipts_dir=tmp_path / "receipts",
        write_receipts=write_receipts,
        verified_at="2026-07-25T15:00:00+00:00",
    )


class TestReconcileMigration:
    def test_pool_dedupes_batch_dois(self, tmp_path: Path):
        dup = make_candidate(status="pending", doi="10.5555/dup",
                             candidate_id="d" * 32)
        dup2 = make_candidate(status="pending", doi="10.5555/dup",
                              candidate_id="e" * 32)
        terminal = make_candidate(status="pending", doi="10.5555/term",
                                  candidate_id="f" * 32, relevance_state="rejected")
        _write_journal(tmp_path / "pending_pages", "p1.json", [dup, dup2, terminal])
        pool = recompute_importable_pool(tmp_path / "pending_pages")
        assert set(pool) == {"d" * 32}
        assert pool["d" * 32].normalized_doi == "10.5555/dup"

    def test_full_closure_all_outcomes(self, tmp_path: Path):
        cand_new = make_candidate(status="pending", doi="10.5555/aaa",
                                  candidate_id="a" * 32)
        cand_dup = make_candidate(status="pending", doi="10.5555/bbb",
                                  candidate_id="b" * 32)
        cand_reused = make_candidate(status="pending", doi="10.5555/ccc",
                                     candidate_id="c" * 32)
        _write_journal(tmp_path / "pending_pages", "p1.json",
                       [cand_new, cand_dup, cand_reused])

        paper_raw = tmp_path / "paper_raw"
        _write_workspace(paper_raw, "0000000000009001", doi="10.5555/aaa",
                         receipt={"candidate_id": "a" * 32,
                                  "normalized_doi": "10.5555/aaa",
                                  "staged_at": AFTER})
        _write_workspace(paper_raw, "0000000000009002", doi="10.5555/bbb",
                         receipt={"candidate_id": "z" * 32,
                                  "normalized_doi": "10.5555/bbb",
                                  "staged_at": BEFORE})
        _write_workspace(paper_raw, "0000000000009003", doi="10.5555/ccc",
                         receipt={"candidate_id": "c" * 32,
                                  "normalized_doi": "10.5555/ccc",
                                  "staged_at": BEFORE})
        _write_ledger(tmp_path / "ledger" / "paper_number_ledger.json", {
            "0000000000009001": {"doi": "10.5555/aaa"},
            "0000000000009002": {"doi": "10.5555/bbb"},
            "0000000000009003": {"doi": "10.5555/ccc"},
        })

        report = _run(tmp_path, expected_imported=3, expected_seeds=3,
                      write_receipts=True)
        assert report.unresolved_items == 0, (
            report.missing, report.conflicting, report.corrupt
        )
        by_outcome = {v.seed.candidate_id: v.outcome for v in report.verdicts}
        assert by_outcome == {
            "a" * 32: "staged_new",
            "b" * 32: "duplicate_existing",
            "c" * 32: "reused_existing",
        }
        store = MigrationReceiptStoreV4(receipts_dir=tmp_path / "receipts")
        assert store.count_seed_receipts() == 3
        receipt = store.read_seed_receipt(
            next(v.seed.seed_id for v in report.verdicts
                 if v.seed.candidate_id == "a" * 32)
        )
        assert receipt is not None
        assert receipt["outcome"] == "staged_new"
        assert receipt["paper_number"] == "0000000000009001"
        assert len(receipt["metadata_sha256"]) == 64
        assert len(receipt["ledger_entry_sha256"]) == 64

        # Idempotent: a second apply re-verifies without conflict.
        again = _run(tmp_path, expected_imported=3, expected_seeds=3,
                     write_receipts=True)
        assert again.unresolved_items == 0
        assert store.count_seed_receipts() == 3

    def test_missing_holder_blocks_closure(self, tmp_path: Path):
        cand = make_candidate(status="pending", doi="10.5555/ghost",
                              candidate_id="a" * 32)
        _write_journal(tmp_path / "pending_pages", "p1.json", [cand])
        (tmp_path / "paper_raw").mkdir()
        _write_ledger(tmp_path / "ledger" / "paper_number_ledger.json", {})

        report = _run(tmp_path, expected_imported=1, expected_seeds=1,
                      write_receipts=True)
        assert len(report.missing) == 1
        assert report.unresolved_items > 0
        assert not (tmp_path / "receipts").exists() or \
            MigrationReceiptStoreV4(receipts_dir=tmp_path / "receipts") \
            .count_seed_receipts() == 0

    def test_pool_size_mismatch_is_corrupt(self, tmp_path: Path):
        cand = make_candidate(status="pending", doi="10.5555/aaa",
                              candidate_id="a" * 32)
        _write_journal(tmp_path / "pending_pages", "p1.json", [cand])
        (tmp_path / "paper_raw").mkdir()
        _write_ledger(tmp_path / "ledger" / "paper_number_ledger.json", {})
        report = _run(tmp_path, expected_imported=5, expected_seeds=5)
        assert any("pool_size" in item for item in report.corrupt)
