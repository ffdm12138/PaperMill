"""Reconcile a finalized discovery v4 migration against real paper_raw.

Recomputes the importable legacy candidate pool from the retained legacy
page journals and proves — per seed, with hashes — that every candidate
landed in ``paper_raw`` (or already existed) with a complete evidence
closure.  Writes one strict receipt per verified seed under
``data/discovery/migrations/<migration_id>.receipts/`` (outside the
manifest-hashed generation tree) and a machine-readable report at
``data/discovery/migrations/<migration_id>.post_cutover_reconciliation.json``.

Default mode is a dry run (no receipts written); pass ``--apply`` to write
receipts.  The command never modifies paper_raw, papers, the ledger, or
the migration journal.  Exit code is non-zero unless every gate passes.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (
    DISCOVERY_DIR,
    DISCOVERY_MIGRATIONS_DIR,
    DISCOVERY_PENDING_PAGES_DIR,
    PAPER_NUMBER_LEDGER_PATH,
    PAPER_RAW_DIR,
    PAPERS_DIR,
)
from src.migrations.discovery_v4.post_cutover_reconciliation import (
    ReconciliationError,
    reconcile_migration,
)


def _default_legacy_pages_dir(migration_id: str) -> Path:
    """Legacy journals: flat dir, else the retained copy for this migration."""
    if DISCOVERY_PENDING_PAGES_DIR.is_dir() and any(DISCOVERY_PENDING_PAGES_DIR.rglob("*.json")):
        return DISCOVERY_PENDING_PAGES_DIR
    retained = (
        DISCOVERY_DIR / "legacy_retained" / migration_id / "pending_pages"
    )
    return retained


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migration-id", required=True)
    parser.add_argument("--legacy-pages-dir", type=Path, default=None)
    parser.add_argument("--apply", action="store_true",
                        help="write per-seed receipts (default: dry run)")
    parser.add_argument("--report-path", type=Path, default=None)
    args = parser.parse_args(argv)

    journal_path = DISCOVERY_MIGRATIONS_DIR / f"{args.migration_id}.json"
    if not journal_path.is_file():
        print(f"[ERROR] migration journal not found: {journal_path}", file=sys.stderr)
        return 2
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if journal.get("state") != "finalized":
        print(f"[ERROR] migration state is {journal.get('state')!r}, "
              f"expected 'finalized'", file=sys.stderr)
        return 2
    stats = journal.get("metadata", {}).get("candidate_stats") or {}
    try:
        expected_imported = int(stats["imported"])
        expected_seeds = int(stats["valid_doi_seeds"])
    except (KeyError, TypeError, ValueError):
        print("[ERROR] journal candidate_stats missing imported/valid_doi_seeds",
              file=sys.stderr)
        return 2
    created_at_raw = journal.get("created_at")
    created_at = datetime.fromisoformat(str(created_at_raw))
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    legacy_pages = args.legacy_pages_dir or _default_legacy_pages_dir(args.migration_id)
    receipts_dir = DISCOVERY_MIGRATIONS_DIR / f"{args.migration_id}.receipts"
    verified_at = datetime.now(timezone.utc).isoformat()

    print(f"[RECONCILE] migration={args.migration_id}")
    print(f"[RECONCILE] legacy pages: {legacy_pages}")
    print(f"[RECONCILE] expected imported={expected_imported} seeds={expected_seeds}")

    try:
        report = reconcile_migration(
            migration_id=args.migration_id,
            migration_created_at=created_at,
            expected_imported=expected_imported,
            expected_valid_doi_seeds=expected_seeds,
            legacy_pages_dir=legacy_pages,
            paper_raw_dir=PAPER_RAW_DIR,
            papers_dir=PAPERS_DIR,
            ledger_path=PAPER_NUMBER_LEDGER_PATH,
            receipts_dir=receipts_dir,
            write_receipts=args.apply,
            verified_at=verified_at,
        )
    except ReconciliationError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    outcomes: dict[str, int] = {}
    for verdict in report.verdicts:
        outcomes[verdict.outcome] = outcomes.get(verdict.outcome, 0) + 1

    payload = {
        "schema_version": "1.0",
        "migration_id": report.migration_id,
        "generated_at": verified_at,
        "mode": "apply" if args.apply else "dry_run",
        "expected_imported": report.expected_imported,
        "expected_valid_doi_seeds": report.expected_valid_doi_seeds,
        "pool_size": report.pool_size,
        "receipts_verified": report.receipts_verified,
        "outcomes": outcomes,
        "missing": report.missing,
        "extra": report.extra,
        "conflicting": report.conflicting,
        "corrupt": report.corrupt,
        "unresolved_items": report.unresolved_items,
        "verdicts": [
            {
                "seed_id": v.seed.seed_id,
                "candidate_id": v.seed.candidate_id,
                "normalized_doi": v.seed.normalized_doi,
                "outcome": v.outcome,
                "paper_number": v.paper_number,
                "problems": v.problems,
            }
            for v in report.verdicts
        ],
    }
    report_path = args.report_path or (
        DISCOVERY_MIGRATIONS_DIR
        / f"{args.migration_id}.post_cutover_reconciliation.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"[RECONCILE] pool={report.pool_size} verified={report.receipts_verified} "
          f"outcomes={outcomes}")
    print(f"[RECONCILE] missing={len(report.missing)} extra={len(report.extra)} "
          f"conflicting={len(report.conflicting)} corrupt={len(report.corrupt)}")
    print(f"[RECONCILE] report: {report_path}")
    if args.apply:
        print(f"[RECONCILE] receipts: {receipts_dir} "
              f"({report.receipts_verified} seeds)")
    if report.unresolved_items:
        print(f"[FAIL] unresolved_items={report.unresolved_items}")
        return 1
    print("[OK] reconciliation closed: every seed has durable evidence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
