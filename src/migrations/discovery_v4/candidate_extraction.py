"""Safe DOI candidate extraction from legacy page journals.

Stream-extracts DOIs from archived journals one file at a time through the
strict :class:`LegacyPageJournalV3` reader, classifies every candidate with
the migration matrix, and deduplicates against the paper ledger, existing
papers, paper_raw workspaces, and previously imported seeds.

Memory safety: known-DOI lookups and batch deduplication live in on-disk
SQLite indexes (``INSERT OR IGNORE``), never in Python sets/lists, so the
full ~1875-file legacy archive can be processed without accumulating DOIs
in memory.

Conservation: every observed candidate is accounted for by exactly one
report counter — ``invalid_doi``, ``already_existing``, ``duplicate_seeds``,
``imported``, ``terminal``, ``quarantined``, or ``unresolved``.
:func:`assert_conservation` is the hard gate; a violation raises
:class:`CandidateConservationError` and the migration must not advance.

Key constraint: legacy seeds are NEVER written as v4 ProviderPageJournals.
They carry ``origin='legacy_candidate_seed'`` and cannot advance cursors
or provide exhaustion evidence.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from src.discovery.models import normalize_doi
from src.migrations.discovery_v4.legacy_contracts.candidate import (
    LegacyCandidateSeedV4,
)
from src.migrations.discovery_v4.legacy_contracts.page_journal_v3 import (
    DISPOSITION_ALREADY_EXISTING,
    DISPOSITION_DUPLICATE,
    DISPOSITION_INVALID,
    DISPOSITION_QUEUE,
    DISPOSITION_RECONCILE,
    DISPOSITION_TERMINAL,
    LegacyCandidateV3,
    classify_legacy_candidate,
    iter_legacy_page_journals,
)
from src.services.metadata_quality import is_valid_normalized_doi


class CandidateConservationError(RuntimeError):
    """The extraction counters violate the conservation equation."""


@dataclass
class CandidateExtractionReport:
    """Summary of legacy candidate extraction.

    Hard conservation equation (enforced by :func:`assert_conservation`)::

        candidates_observed == invalid_doi + already_existing
            + duplicate_seeds + imported + terminal + quarantined
            + unresolved
    """

    journals_scanned: int = 0
    candidates_observed: int = 0
    valid_doi_seeds: int = 0
    invalid_doi: int = 0
    already_existing: int = 0
    duplicate_seeds: int = 0
    imported: int = 0
    terminal: int = 0
    quarantined: int = 0
    unresolved: int = 0
    errors: list[str] = field(default_factory=list)


def assert_conservation(report: CandidateExtractionReport) -> None:
    """Fail closed unless every observed candidate is accounted for."""
    accounted = (
        report.invalid_doi
        + report.already_existing
        + report.duplicate_seeds
        + report.imported
        + report.terminal
        + report.quarantined
        + report.unresolved
    )
    if accounted != report.candidates_observed:
        raise CandidateConservationError(
            f"candidate conservation violated: observed="
            f"{report.candidates_observed} but invalid_doi={report.invalid_doi}"
            f" + already_existing={report.already_existing}"
            f" + duplicate={report.duplicate_seeds}"
            f" + imported={report.imported}"
            f" + terminal={report.terminal}"
            f" + quarantined={report.quarantined}"
            f" + unresolved={report.unresolved}"
            f" = {accounted}"
        )


class SqliteDoiIndex:
    """On-disk DOI index backed by a SQLite database file.

    All membership checks and insertions hit the disk-backed index; no DOI
    accumulation in Python containers.  Not thread-safe by design — the
    extraction pipeline is single-consumer.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS dois (doi TEXT PRIMARY KEY)"
        )
        self._conn.commit()

    def contains(self, normalized_doi: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM dois WHERE doi = ? LIMIT 1", (normalized_doi,)
        ).fetchone()
        return row is not None

    def add(self, normalized_doi: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO dois (doi) VALUES (?)", (normalized_doi,)
        )

    def add_if_absent(self, normalized_doi: str) -> bool:
        """Insert and return True when the DOI was not already present."""
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO dois (doi) VALUES (?)", (normalized_doi,)
        )
        return cursor.rowcount > 0

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM dois").fetchone()
        return int(row[0]) if row else 0

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SqliteDoiIndex":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def build_known_doi_index(
    ledger_path: Path,
    papers_dir: Path,
    paper_raw_dir: Path,
    db_path: Path,
) -> int:
    """Build a disk-backed index of known DOIs from ledger/papers/paper_raw.

    DOI sources follow the real on-disk schemas: Metadata v2.0 records
    (``*.metadata.json``) carry ``doi``; match receipts
    (``*.metadata_match.json``) carry ``requested_doi``; freeze closures only
    store hashes and are never a DOI source.  Streams every source file and
    inserts normalized DOIs with ``INSERT OR IGNORE``; returns the number of
    index entries.  This is a best-effort scan — unreadable source files are
    skipped.
    """
    with SqliteDoiIndex(db_path) as index:
        pending = 0

        def _record(raw_doi: object) -> None:
            nonlocal pending
            if not raw_doi:
                return
            index.add(normalize_doi(str(raw_doi)))
            pending += 1
            if pending >= 1000:
                index.commit()
                pending = 0

        def _scan_metadata(pattern_root: Path, pattern: str, *fields: str) -> None:
            if not pattern_root.is_dir():
                return
            for meta_path in pattern_root.rglob(pattern):
                try:
                    data = json.loads(meta_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                    continue
                if isinstance(data, dict):
                    for field in fields:
                        _record(data.get(field, ""))

        _scan_metadata(paper_raw_dir, "*.metadata.json", "doi")
        _scan_metadata(paper_raw_dir, "*.metadata_match.json", "requested_doi")
        _scan_metadata(papers_dir, "*.metadata.json", "doi")
        _scan_metadata(papers_dir, "*.metadata_match.json", "requested_doi")

        if ledger_path.is_file():
            try:
                ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                ledger = None
            if isinstance(ledger, dict):
                for key in ("entries", "items"):
                    entries = ledger.get(key)
                    if not isinstance(entries, dict):
                        continue
                    for entry in entries.values():
                        if isinstance(entry, dict):
                            _record(entry.get("doi", ""))

        index.commit()
        return index.count()


def stream_journal_candidates(
    journal_dir: Path,
    *,
    stats: dict[str, int] | None = None,
) -> Iterator[LegacyCandidateV3]:
    """Yield strictly validated legacy candidates one journal at a time.

    Corrupt, partial, or schema-violating journals raise
    :class:`LegacyPageJournalContractError` with the file path (fail
    closed).  ``stats["journals_scanned"]`` is incremented once per journal
    file.
    """
    for journal in iter_legacy_page_journals(journal_dir):
        if stats is not None:
            stats["journals_scanned"] = stats.get("journals_scanned", 0) + 1
        yield from journal.candidates


def deduplicate_seeds(
    candidates: Iterator[LegacyCandidateV3],
    *,
    known_doi_index: SqliteDoiIndex,
    batch_index: SqliteDoiIndex,
    stats: dict[str, int],
) -> Iterator[LegacyCandidateSeedV4]:
    """Classify, validate, and deduplicate legacy candidates.

    Applies the migration matrix to every candidate, then for queue/reconcile
    dispositions validates the DOI, checks the known-DOI index, and
    deduplicates within the batch — all through disk-backed SQLite indexes.

    Yields one :class:`LegacyCandidateSeedV4` per unique importable DOI and
    updates ``stats`` with the :class:`CandidateExtractionReport` counters
    ``candidates_observed``, ``invalid_doi``, ``already_existing``,
    ``duplicate_seeds``, ``terminal``, and ``valid_doi_seeds``.
    """

    def _bump(key: str) -> None:
        stats[key] = stats.get(key, 0) + 1

    for candidate in candidates:
        _bump("candidates_observed")
        disposition = classify_legacy_candidate(candidate)

        if disposition == DISPOSITION_TERMINAL:
            _bump("terminal")
            continue
        if disposition == DISPOSITION_INVALID:
            _bump("invalid_doi")
            continue
        if disposition == DISPOSITION_DUPLICATE:
            _bump("duplicate_seeds")
            continue
        if disposition == DISPOSITION_ALREADY_EXISTING:
            _bump("already_existing")
            continue
        if disposition not in (DISPOSITION_QUEUE, DISPOSITION_RECONCILE):
            raise CandidateConservationError(
                f"candidate {candidate.candidate_id!r} mapped to unknown "
                f"disposition {disposition!r}"
            )

        if not candidate.doi:
            _bump("invalid_doi")
            continue
        normalized = normalize_doi(candidate.doi)
        if not is_valid_normalized_doi(normalized):
            _bump("invalid_doi")
            continue

        if known_doi_index.contains(normalized):
            _bump("already_existing")
            continue
        if not batch_index.add_if_absent(normalized):
            _bump("duplicate_seeds")
            continue

        _bump("valid_doi_seeds")
        yield LegacyCandidateSeedV4(
            seed_id=LegacyCandidateSeedV4.compute_seed_id(
                candidate.page_id, normalized
            ),
            doi=candidate.doi,
            normalized_doi=normalized,
            keyword_id=candidate.keyword_id,
            keyword_zh=candidate.keyword_zh,
            query_id=candidate.query_id,
            query_language=candidate.query_language,
            title=candidate.title,
            provider=candidate.provider,
            legacy_page_id=candidate.page_id,
            legacy_journal_sha256=candidate.journal_sha256,
            source_schema_version=candidate.source_schema_version,
            legacy_candidate_id=candidate.candidate_id,
            lane=candidate.lane,
            query=candidate.query,
            authors=candidate.authors,
            year=candidate.year,
            venue=candidate.venue,
        )


def stream_extract_candidates(
    journal_dir: Path,
    *,
    known_doi_index: SqliteDoiIndex,
    batch_index: SqliteDoiIndex,
    stats: dict[str, int],
) -> Iterator[LegacyCandidateSeedV4]:
    """Full streaming pipeline: strict read → matrix classify → dedupe.

    Yields :class:`LegacyCandidateSeedV4` one at a time; never loads more
    than one journal file into memory and never accumulates DOIs in Python
    containers.
    """
    candidates = stream_journal_candidates(journal_dir, stats=stats)
    yield from deduplicate_seeds(
        candidates,
        known_doi_index=known_doi_index,
        batch_index=batch_index,
        stats=stats,
    )
