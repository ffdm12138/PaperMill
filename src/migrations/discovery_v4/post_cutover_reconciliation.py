"""Post-cutover reconciliation for the discovery v4 migration.

Recomputes the importable legacy candidate pool from the retained legacy
page journals with the exact migration contract (strict journal parsing,
migration matrix, DOI validation, SQLite batch deduplication) and proves —
per seed — that the candidate actually landed in ``paper_raw`` (or was
already present) with a complete evidence closure:

``metadata v2.0 · discovery receipt · stage manifest · import status ·
ledger entry`` (``metadata_match`` recorded when present; it legitimately
does not exist before PDF attachment).

Outcomes per seed:

* ``staged_new`` — a discovery receipt whose ``candidate_id`` is exactly
  the legacy candidate id holds the seed DOI (drain wrote it).
* ``duplicate_existing`` — no such receipt, but the DOI is held by a
  workspace whose evidence predates the migration (the staging
  transaction's duplicate guard converged the candidate).
* ``reused_existing`` — reserved for drains that reuse a workspace and
  still write the candidate's receipt.
* ``terminal_with_evidence`` — reserved for operator-resolved terminals.

The word "missing" is never inferred from an absent pending file; every
seed requires positive on-disk evidence.  All counting gates read the
expected numbers from the migration journal — nothing is hardcoded.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from src.discovery.models import normalize_doi
from src.discovery.stores.migration_receipt_store import (
    MigrationReceiptStoreV4,
    SEED_RECEIPT_SCHEMA_VERSION,
)
from src.migrations.discovery_v4.candidate_extraction import (
    SqliteDoiIndex,
    stream_journal_candidates,
)
from src.migrations.discovery_v4.legacy_contracts.candidate import (
    LegacyCandidateSeedV4,
)
from src.migrations.discovery_v4.legacy_contracts.page_journal_v3 import (
    DISPOSITION_QUEUE,
    DISPOSITION_RECONCILE,
    classify_legacy_candidate,
)
from src.services.metadata_quality import is_valid_normalized_doi


class ReconciliationError(RuntimeError):
    """Reconciliation could not produce a trustworthy verdict."""


@dataclass(frozen=True)
class PoolSeed:
    """One recomputed importable legacy candidate."""

    seed_id: str
    candidate_id: str
    page_id: str
    normalized_doi: str
    keyword_id: str


@dataclass
class SeedVerdict:
    """Per-seed reconciliation outcome with evidence hashes."""

    seed: PoolSeed
    outcome: str
    paper_number: str = ""
    metadata_sha256: str = ""
    match_receipt_sha256: str = ""
    ledger_entry_sha256: str = ""
    problems: list[str] = field(default_factory=list)


@dataclass
class ReconciliationReport:
    """Machine-readable reconciliation result."""

    migration_id: str
    expected_imported: int
    expected_valid_doi_seeds: int
    pool_size: int = 0
    receipts_verified: int = 0
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    conflicting: list[str] = field(default_factory=list)
    corrupt: list[str] = field(default_factory=list)
    verdicts: list[SeedVerdict] = field(default_factory=list)

    @property
    def unresolved_items(self) -> int:
        return (
            len(self.missing)
            + len(self.extra)
            + len(self.conflicting)
            + len(self.corrupt)
        )


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_json(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def recompute_importable_pool(legacy_pages_dir: Path) -> dict[str, PoolSeed]:
    """Replay the migration candidate contract over the legacy journals.

    Identical to the migration extraction step except that the known-DOI
    filter is deliberately omitted: whether a seed already existed is
    decided here by positive holder evidence, not by an index that must be
    reconstructed as-of a past timestamp.  Returns ``candidate_id -> seed``
    for every QUEUE/RECONCILE candidate with a valid, batch-unique DOI.
    """
    pool: dict[str, PoolSeed] = {}
    with tempfile.TemporaryDirectory(prefix="mineru_v4_reconcile_") as tmp:
        with SqliteDoiIndex(Path(tmp) / "batch_dois.sqlite") as batch_index:
            for candidate in stream_journal_candidates(legacy_pages_dir):
                disposition = classify_legacy_candidate(candidate)
                if disposition not in (DISPOSITION_QUEUE, DISPOSITION_RECONCILE):
                    continue
                if not candidate.doi:
                    continue
                normalized = normalize_doi(candidate.doi)
                if not is_valid_normalized_doi(normalized):
                    continue
                if not batch_index.add_if_absent(normalized):
                    continue
                pool[candidate.candidate_id] = PoolSeed(
                    seed_id=LegacyCandidateSeedV4.compute_seed_id(
                        candidate.page_id, normalized
                    ),
                    candidate_id=candidate.candidate_id,
                    page_id=candidate.page_id,
                    normalized_doi=normalized,
                    keyword_id=candidate.keyword_id,
                )
    return pool


@dataclass
class _HolderEvidence:
    """All on-disk evidence for one DOI holder workspace."""

    paper_number: str
    workspace_dir: Path
    kind: str  # "paper_raw" | "papers"
    receipts: list[dict[str, Any]] = field(default_factory=list)
    metadata_path: Path | None = None
    match_path: Path | None = None
    stage_manifest_path: Path | None = None
    import_status_path: Path | None = None


def _iter_holder_dirs(paper_raw_dir: Path, papers_dir: Path) -> Iterator[tuple[str, Path, str]]:
    if paper_raw_dir.is_dir():
        for child in sorted(paper_raw_dir.iterdir()):
            if child.is_dir():
                yield child.name, child, "paper_raw"
    if papers_dir.is_dir():
        for child in sorted(papers_dir.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                yield child.name, child, "papers"


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def build_doi_evidence_index(
    paper_raw_dir: Path,
    papers_dir: Path,
    wanted_dois: set[str],
) -> dict[str, list[_HolderEvidence]]:
    """Index every workspace holding one of ``wanted_dois``.

    Sources mirror the real on-disk schemas (Metadata v2.0 ``identifiers.doi``,
    match receipt ``requested_doi``, discovery receipt ``normalized_doi``).
    Only DOIs in ``wanted_dois`` are tracked so the scan stays bounded.
    """
    index: dict[str, list[_HolderEvidence]] = {}
    receipts_by_dir: dict[Path, list[dict[str, Any]]] = {}
    meta_by_dir: dict[Path, Path] = {}
    match_by_dir: dict[Path, Path] = {}

    def _holder(ws_dir: Path, kind: str, paper_number: str) -> _HolderEvidence:
        return _HolderEvidence(
            paper_number=paper_number,
            workspace_dir=ws_dir,
            kind=kind,
            receipts=receipts_by_dir.get(ws_dir, []),
            metadata_path=meta_by_dir.get(ws_dir),
            match_path=match_by_dir.get(ws_dir),
            stage_manifest_path=(
                ws_dir / "stage_manifest.json"
                if (ws_dir / "stage_manifest.json").is_file()
                else None
            ),
            import_status_path=(
                ws_dir / ".import_status.json"
                if (ws_dir / ".import_status.json").is_file()
                else None
            ),
        )

    holders: dict[Path, tuple[str, str, set[str]]] = {}  # ws -> (pn, kind, dois)

    def _note(ws_dir: Path, paper_number: str, kind: str, doi: str) -> None:
        entry = holders.setdefault(ws_dir, (paper_number, kind, set()))
        entry[2].add(doi)

    for paper_number, ws_dir, kind in _iter_holder_dirs(paper_raw_dir, papers_dir):
        for receipt_path in ws_dir.glob("*.discovery_receipt.json"):
            data = _load_json(receipt_path)
            if data is None:
                continue
            data = dict(data)
            data["_path"] = receipt_path
            receipts_by_dir.setdefault(ws_dir, []).append(data)
            doi = normalize_doi(str(data.get("normalized_doi") or ""))
            if doi in wanted_dois:
                _note(ws_dir, str(data.get("paper_number") or paper_number), kind, doi)
        for meta_path in ws_dir.glob("*.metadata.json"):
            data = _load_json(meta_path)
            if data is None:
                continue
            meta_by_dir[ws_dir] = meta_path
            doi = normalize_doi(str((data.get("identifiers") or {}).get("doi") or ""))
            if doi in wanted_dois:
                _note(ws_dir, paper_number, kind, doi)
        for match_path in ws_dir.glob("*.metadata_match.json"):
            data = _load_json(match_path)
            if data is None:
                continue
            match_by_dir[ws_dir] = match_path
            doi = normalize_doi(str(data.get("requested_doi") or ""))
            if doi in wanted_dois:
                _note(ws_dir, paper_number, kind, doi)

    for ws_dir, (paper_number, kind, dois) in holders.items():
        holder = _holder(ws_dir, kind, paper_number)
        for doi in dois:
            index.setdefault(doi, []).append(holder)
    return index


def _holder_temporal_evidence(holder: _HolderEvidence) -> datetime | None:
    """Earliest trustworthy timestamp for a holder workspace."""
    times: list[datetime] = []
    for receipt in holder.receipts:
        parsed = _parse_time(receipt.get("staged_at"))
        if parsed is not None:
            times.append(parsed)
    if holder.import_status_path is not None:
        data = _load_json(holder.import_status_path)
        if data:
            parsed = _parse_time(data.get("updated_at"))
            if parsed is not None:
                times.append(parsed)
    if not times and holder.metadata_path is not None:
        try:
            times.append(
                datetime.fromtimestamp(
                    holder.metadata_path.stat().st_mtime, timezone.utc
                )
            )
        except OSError:
            pass
    return min(times) if times else None


def _verify_closure(
    seed: PoolSeed,
    holder: _HolderEvidence,
    ledger_entries: dict[str, Any],
) -> tuple[list[str], str, str, str]:
    """Verify the evidence closure; returns (problems, sha triple)."""
    problems: list[str] = []
    metadata_sha = ""
    match_sha = ""
    ledger_sha = ""

    if holder.metadata_path is None:
        problems.append("metadata_missing")
    else:
        data = _load_json(holder.metadata_path)
        if data is None:
            problems.append("metadata_corrupt")
        else:
            if str(data.get("schema_version")) != "2.0":
                problems.append("metadata_schema_not_v2")
            held = normalize_doi(str((data.get("identifiers") or {}).get("doi") or ""))
            if held != seed.normalized_doi:
                problems.append("metadata_doi_mismatch")
            metadata_sha = _sha256_file(holder.metadata_path)

    if holder.kind == "paper_raw":
        if holder.stage_manifest_path is None:
            problems.append("stage_manifest_missing")
        elif _load_json(holder.stage_manifest_path) is None:
            problems.append("stage_manifest_corrupt")
        if holder.import_status_path is None:
            problems.append("import_status_missing")
        elif _load_json(holder.import_status_path) is None:
            problems.append("import_status_corrupt")
        if not holder.receipts:
            problems.append("discovery_receipt_missing")

    if holder.match_path is not None:
        match_sha = _sha256_file(holder.match_path)

    entry = ledger_entries.get(holder.paper_number)
    if entry is None:
        problems.append("ledger_entry_missing")
    else:
        ledger_doi = normalize_doi(str(entry.get("doi") or "")) if isinstance(entry, dict) else ""
        if isinstance(entry, dict) and entry.get("doi") and ledger_doi != seed.normalized_doi:
            problems.append("ledger_doi_mismatch")
        ledger_sha = _sha256_json(entry)

    return problems, metadata_sha, match_sha, ledger_sha


def reconcile_migration(
    *,
    migration_id: str,
    migration_created_at: datetime,
    expected_imported: int,
    expected_valid_doi_seeds: int,
    legacy_pages_dir: Path,
    paper_raw_dir: Path,
    papers_dir: Path,
    ledger_path: Path,
    receipts_dir: Path,
    write_receipts: bool,
    verified_at: str,
) -> ReconciliationReport:
    """Run the full post-cutover reconciliation.

    When ``write_receipts`` is true, one strict seed receipt per verified
    seed is created under ``receipts_dir`` (create-if-absent).  The report
    is returned either way; gates are evaluated by the caller via
    :attr:`ReconciliationReport.unresolved_items`.
    """
    if not legacy_pages_dir.is_dir():
        raise ReconciliationError(
            f"legacy page journals not found: {legacy_pages_dir}"
        )
    ledger_data = _load_json(ledger_path)
    if ledger_data is None:
        raise ReconciliationError(f"paper number ledger unreadable: {ledger_path}")
    ledger_entries = ledger_data.get("entries")
    if not isinstance(ledger_entries, dict):
        ledger_entries = ledger_data.get("items")
    if not isinstance(ledger_entries, dict):
        raise ReconciliationError(
            f"paper number ledger has no entries mapping: {ledger_path}"
        )

    pool = recompute_importable_pool(legacy_pages_dir)
    report = ReconciliationReport(
        migration_id=migration_id,
        expected_imported=expected_imported,
        expected_valid_doi_seeds=expected_valid_doi_seeds,
        pool_size=len(pool),
    )
    if len(pool) != expected_valid_doi_seeds:
        report.corrupt.append(
            f"pool_size={len(pool)} != journal valid_doi_seeds="
            f"{expected_valid_doi_seeds}"
        )

    wanted = {seed.normalized_doi for seed in pool.values()}
    index = build_doi_evidence_index(paper_raw_dir, papers_dir, wanted)
    store = MigrationReceiptStoreV4(receipts_dir=receipts_dir)

    for candidate_id in sorted(pool):
        seed = pool[candidate_id]
        verdict = SeedVerdict(seed=seed, outcome="")
        holders = index.get(seed.normalized_doi, [])

        exact_holder: _HolderEvidence | None = None
        exact_receipt: dict[str, Any] | None = None
        for holder in holders:
            for receipt in holder.receipts:
                if receipt.get("candidate_id") != seed.candidate_id:
                    continue
                receipt_doi = normalize_doi(str(receipt.get("normalized_doi") or ""))
                if receipt_doi != seed.normalized_doi:
                    verdict.outcome = "conflicting"
                    verdict.problems.append("receipt_doi_mismatch")
                else:
                    exact_holder = holder
                    exact_receipt = receipt
                break
            if exact_holder is not None or verdict.outcome == "conflicting":
                break

        if verdict.outcome != "conflicting":
            if exact_holder is not None:
                staged_at = _parse_time((exact_receipt or {}).get("staged_at"))
                if staged_at is not None and staged_at >= migration_created_at:
                    verdict.outcome = "staged_new"
                else:
                    verdict.outcome = "reused_existing"
            elif holders:
                # Duplicate convergence: the staging duplicate guard kept the
                # pre-existing workspace.  Require evidence predating the
                # migration so a fresh mis-staging cannot masquerade.
                preexisting = [
                    holder for holder in holders
                    if (ts := _holder_temporal_evidence(holder)) is not None
                    and ts < migration_created_at
                ]
                if preexisting:
                    exact_holder = preexisting[0]
                    verdict.outcome = "duplicate_existing"
                else:
                    verdict.outcome = "conflicting"
                    verdict.problems.append("holder_evidence_not_premigration")
            else:
                verdict.outcome = "missing"

        if exact_holder is not None:
            verdict.paper_number = exact_holder.paper_number
            problems, metadata_sha, match_sha, ledger_sha = _verify_closure(
                seed, exact_holder, ledger_entries
            )
            verdict.metadata_sha256 = metadata_sha
            verdict.match_receipt_sha256 = match_sha
            verdict.ledger_entry_sha256 = ledger_sha
            verdict.problems.extend(problems)

        report.verdicts.append(verdict)

    for verdict in report.verdicts:
        if verdict.outcome == "missing":
            report.missing.append(verdict.seed.seed_id)
        elif verdict.outcome == "conflicting" or verdict.problems:
            report.conflicting.append(
                f"{verdict.seed.seed_id}:{verdict.outcome}:"
                f"{','.join(verdict.problems)}"
            )

    verified = [
        v for v in report.verdicts
        if v.outcome in {"staged_new", "reused_existing", "duplicate_existing"}
        and not v.problems
    ]
    report.receipts_verified = len(verified)
    if report.receipts_verified != expected_imported:
        report.corrupt.append(
            f"verified={report.receipts_verified} != journal imported="
            f"{expected_imported}"
        )

    if write_receipts:
        for verdict in verified:
            store.write_seed_receipt({
                "schema_version": SEED_RECEIPT_SCHEMA_VERSION,
                "migration_id": migration_id,
                "seed_id": verdict.seed.seed_id,
                "candidate_id": verdict.seed.candidate_id,
                "normalized_doi": verdict.seed.normalized_doi,
                "outcome": verdict.outcome,
                "paper_number": verdict.paper_number,
                "metadata_sha256": verdict.metadata_sha256,
                "match_receipt_sha256": verdict.match_receipt_sha256,
                "ledger_entry_sha256": verdict.ledger_entry_sha256,
                "verified_at": verified_at,
            })

    if receipts_dir.is_dir():
        pool_seed_ids = {seed.seed_id for seed in pool.values()}
        for receipt_path in sorted(receipts_dir.glob("*.json")):
            if receipt_path.stem not in pool_seed_ids:
                report.extra.append(receipt_path.stem)

    return report
