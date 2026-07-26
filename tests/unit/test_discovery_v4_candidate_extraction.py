"""Strict legacy page journal reader + candidate extraction pipeline tests.

Covers the strict ``LegacyPageJournalV3`` reader, the migration matrix,
the conservation gate, the quarantine flow, and the disk-backed (SQLite)
deduplication path.  All fixtures mirror the real production journal
schema (schema_version "2.0", 22 top-level keys).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.migrations.discovery_v4.candidate_extraction import (
    CandidateExtractionReport,
    SqliteDoiIndex,
    build_known_doi_index,
    stream_extract_candidates,
)
from src.migrations.discovery_v4.legacy_contracts.page_journal_v3 import (
    DISPOSITION_ALREADY_EXISTING,
    DISPOSITION_DUPLICATE,
    DISPOSITION_INVALID,
    DISPOSITION_QUEUE,
    DISPOSITION_RECONCILE,
    DISPOSITION_TERMINAL,
    LegacyPageJournalContractError,
    LegacyPageJournalV3,
    classify_legacy_candidate,
    iter_legacy_page_journals,
)
from tests.helpers.legacy_journals import make_candidate, make_journal

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "discovery_v4_legacy"
JOURNAL_FIXTURES = FIXTURES / "page_journals"


# ── Strict reader ─────────────────────────────────────────────────────────


class TestStrictReader:
    def test_valid_journal_parses_with_provenance(self):
        journal = LegacyPageJournalV3.from_file(
            JOURNAL_FIXTURES / "matrix" / "pending_passed.json"
        )
        assert journal.schema_version == "2.0"
        assert journal.state == "drained"
        assert len(journal.journal_sha256) == 64
        assert len(journal.candidates) == 1
        cand = journal.candidates[0]
        assert cand.status == "pending"
        assert cand.relevance_state == "passed"
        assert cand.doi == "10.5555/matrix-passed"
        assert cand.journal_sha256 == journal.journal_sha256
        assert cand.source_schema_version == "2.0"

    def test_corrupt_journal_fails_with_path(self):
        path = JOURNAL_FIXTURES / "negative" / "corrupt.json"
        with pytest.raises(LegacyPageJournalContractError) as excinfo:
            LegacyPageJournalV3.from_file(path)
        assert str(path) in str(excinfo.value)
        assert "corrupt JSON" in str(excinfo.value)

    def test_unknown_status_fails_closed(self):
        path = JOURNAL_FIXTURES / "negative" / "unknown_status.json"
        with pytest.raises(LegacyPageJournalContractError) as excinfo:
            LegacyPageJournalV3.from_file(path)
        assert str(path) in str(excinfo.value)
        assert "status" in str(excinfo.value)

    def test_partial_journal_fails_closed(self):
        path = JOURNAL_FIXTURES / "negative" / "partial_journal.json"
        with pytest.raises(LegacyPageJournalContractError) as excinfo:
            LegacyPageJournalV3.from_file(path)
        assert str(path) in str(excinfo.value)
        assert "missing keys" in str(excinfo.value)

    def test_unknown_top_level_field_fails_closed(self):
        path = JOURNAL_FIXTURES / "negative" / "unknown_top_level_field.json"
        with pytest.raises(LegacyPageJournalContractError) as excinfo:
            LegacyPageJournalV3.from_file(path)
        assert "unknown keys" in str(excinfo.value)

    def test_unsupported_schema_fails_closed(self):
        path = JOURNAL_FIXTURES / "negative" / "unsupported_schema.json"
        with pytest.raises(LegacyPageJournalContractError) as excinfo:
            LegacyPageJournalV3.from_file(path)
        assert "schema_version" in str(excinfo.value)

    def test_unknown_candidate_wrapper_field_fails_closed(self):
        journal = make_journal(
            [make_candidate("pending", "10.5555/x", candidate_id="c" * 32)]
        )
        journal["candidates"][0]["surprise"] = 1
        with pytest.raises(LegacyPageJournalContractError, match="unknown keys"):
            LegacyPageJournalV3.from_dict_strict(journal)

    def test_unknown_inner_candidate_field_fails_closed(self):
        journal = make_journal(
            [make_candidate("pending", "10.5555/x", candidate_id="c" * 32)]
        )
        journal["candidates"][0]["candidate"]["surprise"] = 1
        with pytest.raises(LegacyPageJournalContractError, match="unknown keys"):
            LegacyPageJournalV3.from_dict_strict(journal)

    def test_iterator_skips_co_located_archive_manifest(self, tmp_path):
        journal_dir = tmp_path / "pending_pages"
        lane_dir = journal_dir / "kw" / "q" / "openalex" / "backfill"
        lane_dir.mkdir(parents=True)
        (lane_dir / "page1.json").write_text(
            json.dumps(
                make_journal(
                    [make_candidate("pending", "10.5555/x", candidate_id="c" * 32)]
                )
            ),
            encoding="utf-8",
        )
        # Archive snapshots co-locate their manifest with the journals.
        (journal_dir / "archive_manifest.json").write_text(
            json.dumps({"files": []}), encoding="utf-8"
        )
        journals = list(iter_legacy_page_journals(journal_dir))
        assert len(journals) == 1
        assert journals[0].candidates[0].doi == "10.5555/x"


# ── Migration matrix (one row per real legacy status) ─────────────────────


class TestMigrationMatrix:
    @staticmethod
    def _disposition(fixture: str) -> str:
        journal = LegacyPageJournalV3.from_file(JOURNAL_FIXTURES / "matrix" / fixture)
        assert len(journal.candidates) == 1
        return classify_legacy_candidate(journal.candidates[0])

    def test_pending_with_relevance_passed_queues(self):
        assert self._disposition("pending_passed.json") == DISPOSITION_QUEUE

    def test_pending_without_relevance_queues(self):
        assert self._disposition("pending_no_relevance.json") == DISPOSITION_QUEUE

    def test_pending_with_relevance_rejected_is_terminal(self):
        assert self._disposition("pending_rejected.json") == DISPOSITION_TERMINAL

    def test_processing_lease_is_retryable_and_queues(self):
        assert self._disposition("processing.json") == DISPOSITION_QUEUE

    def test_staged_with_durable_receipt_is_already_existing(self):
        assert self._disposition("staged_with_receipt.json") == DISPOSITION_ALREADY_EXISTING

    def test_staged_without_receipt_reconciles(self):
        assert self._disposition("staged_no_receipt.json") == DISPOSITION_RECONCILE

    def test_emitted_unreconciled_reconciles(self):
        assert self._disposition("emitted_unreconciled.json") == DISPOSITION_RECONCILE

    def test_emitted_reconciled_is_already_existing(self):
        assert self._disposition("emitted_reconciled.json") == DISPOSITION_ALREADY_EXISTING

    def test_existing_duplicate_is_already_existing(self):
        assert self._disposition("existing_duplicate.json") == DISPOSITION_ALREADY_EXISTING

    def test_duplicate_observation_is_duplicate(self):
        assert self._disposition("duplicate_observation.json") == DISPOSITION_DUPLICATE

    def test_unresolved_doi_is_invalid(self):
        assert self._disposition("unresolved.json") == DISPOSITION_INVALID


# ── Extraction pipeline (dedupe + reconciliation + counting) ──────────────


@pytest.fixture
def doi_indexes(tmp_path):
    with SqliteDoiIndex(tmp_path / "known.sqlite") as known, \
            SqliteDoiIndex(tmp_path / "batch.sqlite") as batch:
        yield known, batch


def _run_pipeline(journal_dir: Path, known: SqliteDoiIndex, batch: SqliteDoiIndex):
    stats: dict[str, int] = {}
    seeds = list(stream_extract_candidates(
        journal_dir, known_doi_index=known, batch_index=batch, stats=stats,
    ))
    return seeds, stats


class TestExtractionPipeline:
    def test_matrix_directory_counts_every_row(self, doi_indexes):
        known, batch = doi_indexes
        seeds, stats = _run_pipeline(JOURNAL_FIXTURES / "matrix", known, batch)

        # queue: pending_passed, pending_no_relevance, processing
        #        + reconcile-unknown: staged_no_receipt, emitted_unreconciled
        assert stats["candidates_observed"] == 11
        assert stats["valid_doi_seeds"] == 5
        assert stats["terminal"] == 1            # relevance rejected
        assert stats["already_existing"] == 3    # staged+receipt, emitted reconciled, existing_duplicate
        assert stats["duplicate_seeds"] == 1     # duplicate_observation
        assert stats["invalid_doi"] == 1         # unresolved
        assert len(seeds) == 5
        assert stats["journals_scanned"] == 11

    def test_reconcile_hit_in_known_index_becomes_already_existing(self, doi_indexes):
        known, batch = doi_indexes
        known.add("10.5555/matrix-staged-noreceipt")
        known.commit()
        seeds, stats = _run_pipeline(
            JOURNAL_FIXTURES / "matrix", known, batch,
        )
        # staged_no_receipt now resolves against the known-DOI index.
        assert stats["already_existing"] == 4
        assert stats["valid_doi_seeds"] == 4
        assert len(seeds) == 4

    def test_duplicate_doi_across_journals_counted_once(self, doi_indexes):
        known, batch = doi_indexes
        seeds, stats = _run_pipeline(JOURNAL_FIXTURES / "duplicates", known, batch)
        assert stats["candidates_observed"] == 2
        assert stats["valid_doi_seeds"] == 1
        assert stats["duplicate_seeds"] == 1
        assert len(seeds) == 1
        assert seeds[0].normalized_doi == "10.5555/dup-shared"

    def test_invalid_doi_format_counted(self, tmp_path, doi_indexes):
        known, batch = doi_indexes
        journal = make_journal(
            [make_candidate("pending", "not-a-doi", candidate_id="c" * 32)]
        )
        pages = tmp_path / "pages"
        pages.mkdir()
        (pages / "p1.json").write_text(json.dumps(journal), encoding="utf-8")
        seeds, stats = _run_pipeline(pages, known, batch)
        assert stats["invalid_doi"] == 1
        assert seeds == []

    def test_seed_carries_full_provenance(self, doi_indexes):
        known, batch = doi_indexes
        seeds, _ = _run_pipeline(
            JOURNAL_FIXTURES / "matrix", known, batch,
        )
        seed = next(s for s in seeds if s.normalized_doi == "10.5555/matrix-passed")
        assert seed.seed_id
        assert len(seed.legacy_journal_sha256) == 64
        assert seed.source_schema_version == "2.0"
        assert seed.legacy_page_id
        assert seed.keyword_id == "a" * 16
        assert seed.legacy_candidate_id.startswith("c")
        roundtrip = type(seed).from_dict_strict(seed.to_dict())
        assert roundtrip == seed


class TestSqliteBackedDedupe:
    def test_pipeline_uses_disk_index_not_python_sets(self, tmp_path):
        """Large generated input is deduplicated through the SQLite file."""
        pages = tmp_path / "pages"
        pages.mkdir()
        n_journals = 40
        per_journal = 25
        for i in range(n_journals):
            cands = [
                make_candidate(
                    "pending", f"10.5555/bulk.{i:03d}.{k:03d}",
                    candidate_id=f"c{i:03d}{k:03d}" + "0" * 25,
                )
                for k in range(per_journal)
            ]
            # Every journal repeats the first DOI of the previous journal.
            if i:
                cands.append(make_candidate(
                    "pending", f"10.5555/bulk.{i - 1:03d}.000",
                    candidate_id=f"c{i:03d}dup" + "0" * 25,
                ))
            (pages / f"p{i:03d}.json").write_text(
                json.dumps(make_journal(cands), ensure_ascii=False), encoding="utf-8"
            )

        with SqliteDoiIndex(tmp_path / "known.sqlite") as known, \
                SqliteDoiIndex(tmp_path / "batch.sqlite") as batch:
            stats: dict[str, int] = {}
            seed_stream = stream_extract_candidates(
                pages, known_doi_index=known, batch_index=batch, stats=stats,
            )
            assert iter(seed_stream) is seed_stream  # generator, not a list
            seeds = list(seed_stream)
            assert stats["candidates_observed"] == n_journals * per_journal + (n_journals - 1)
            assert stats["valid_doi_seeds"] == n_journals * per_journal
            assert stats["duplicate_seeds"] == n_journals - 1
            assert len(seeds) == n_journals * per_journal
            # The batch dedupe index lives on disk, not in a Python set.
            assert (tmp_path / "batch.sqlite").is_file()
            assert batch.count() == n_journals * per_journal

    def test_add_if_absent_semantics(self, tmp_path):
        with SqliteDoiIndex(tmp_path / "idx.sqlite") as index:
            assert index.add_if_absent("10.5555/a") is True
            assert index.add_if_absent("10.5555/a") is False
            assert index.contains("10.5555/a")
            assert not index.contains("10.5555/b")


class TestKnownDoiIndex:
    def test_builds_from_ledger_papers_and_paper_raw(self, tmp_path):
        paper_raw = tmp_path / "paper_raw"
        papers = tmp_path / "papers"
        (paper_raw / "0000000000000001").mkdir(parents=True)
        # Metadata v2.0 carries the DOI.
        (paper_raw / "0000000000000001" / "0000000000000001.metadata.json").write_text(
            json.dumps({"doi": "10.5555/known-raw"}), encoding="utf-8"
        )
        # Match receipts carry the requested DOI.
        (paper_raw / "0000000000000001" / "0000000000000001.metadata_match.json").write_text(
            json.dumps({"requested_doi": "10.5555/known-match"}), encoding="utf-8"
        )
        # Freeze closures only store hashes — never a DOI source.
        (paper_raw / "0000000000000001" / "0000000000000001.metadata_freeze.json").write_text(
            json.dumps({"metadata_sha256": "abc", "pdf_sha256": "def"}), encoding="utf-8"
        )
        (papers / "paper_x").mkdir(parents=True)
        (papers / "paper_x" / "paper_x.metadata.json").write_text(
            json.dumps({"doi": "10.5555/known-paper"}), encoding="utf-8"
        )
        ledger = tmp_path / "ledger.json"
        ledger.write_text(
            json.dumps({"items": {"e1": {"doi": "10.5555/known-ledger"}}}),
            encoding="utf-8",
        )
        count = build_known_doi_index(
            ledger, papers, paper_raw, tmp_path / "known.sqlite",
        )
        assert count == 4
        with SqliteDoiIndex(tmp_path / "known.sqlite") as index:
            for doi in ("10.5555/known-raw", "10.5555/known-match",
                        "10.5555/known-paper", "10.5555/known-ledger"):
                assert index.contains(doi)


# ── Conservation gate ─────────────────────────────────────────────────────


class TestConservation:
    """The conservation gate must hold for any counter combination.

    Exception classes are looked up through the module (not imported names)
    because the migrate-CLI test fixture reloads the extraction module.
    """

    @staticmethod
    def _module():
        from src.migrations.discovery_v4 import candidate_extraction

        return candidate_extraction

    def test_balanced_report_passes(self):
        report = CandidateExtractionReport(
            candidates_observed=10,
            invalid_doi=1,
            already_existing=2,
            duplicate_seeds=1,
            imported=3,
            terminal=1,
            quarantined=1,
            unresolved=1,
        )
        self._module().assert_conservation(report)

    def test_unbalanced_report_fails_closed(self):
        module = self._module()
        report = CandidateExtractionReport(candidates_observed=10, imported=9)
        with pytest.raises(
            module.CandidateConservationError, match="conservation violated"
        ):
            module.assert_conservation(report)

    def test_pipeline_counters_conserve(self, doi_indexes):
        known, batch = doi_indexes
        _, stats = _run_pipeline(JOURNAL_FIXTURES / "matrix", known, batch)
        accounted = (
            stats["invalid_doi"] + stats["already_existing"]
            + stats["duplicate_seeds"] + stats["valid_doi_seeds"]
            + stats["terminal"]
        )
        assert accounted == stats["candidates_observed"]


# ── Removed operator flag ─────────────────────────────────────────────────


class TestSkipFlagRemoved:
    def test_removed_operator_flag_is_gone_everywhere(self):
        """The operator flag must have zero hits in src/scripts/tests."""
        needle = "--skip-{}-{}".format("candidate", "extraction")
        snake = "skip{}candidate{}extraction".format("_", "_")
        repo = Path(__file__).resolve().parent.parent.parent
        offenders: list[str] = []
        for base in ("src", "scripts", "tests"):
            for pyfile in sorted((repo / base).rglob("*.py")):
                if pyfile.resolve() == Path(__file__).resolve():
                    continue
                text = pyfile.read_text(encoding="utf-8")
                if needle in text or snake in text:
                    offenders.append(str(pyfile.relative_to(repo)))
        assert offenders == []
