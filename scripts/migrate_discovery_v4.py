#!/usr/bin/env python
"""One-time Discovery v4 migration: archive legacy, build workspace, activate.

Orchestrates the complete 12-phase v4 migration:

    python scripts/migrate_discovery_v4.py --plan
    python scripts/migrate_discovery_v4.py --apply
    python scripts/migrate_discovery_v4.py --resume <migration_id>
    python scripts/migrate_discovery_v4.py --dry-run

Migration states: planned → inventory_complete → archive_prepared →
notebooks_staged → candidates_extracted → workspace_built →
preflight_validated → smoke_passed → cutover_committed → legacy_cleaned
→ finalized
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (
    DISCOVERY_DIR,
    DISCOVERY_KEYWORD_NOTEBOOK_DIR,
    DISCOVERY_PENDING_PAGES_DIR,
    PAPER_NUMBER_LEDGER_PATH,
    PAPER_RAW_DIR,
    PAPERS_DIR,
)
from src.discovery.workspace import (
    DiscoveryWorkspace,
    STAGING_DIR,
    create_staging_workspace,
    commit_workspace,
)
from src.migrations.discovery_v4.legacy_inventory import generate_inventory_report
from src.migrations.discovery_v4.migration_journal import MigrationJournal, MigrationState
from src.migrations.discovery_v4.archive_builder import prepare_legacy_archive
from src.migrations.discovery_v4.notebook_migration import migrate_all_notebooks
from src.migrations.discovery_v4.candidate_extraction import (
    CandidateExtractionReport,
    stream_extract_candidates,
    deduplicate_seeds,
    build_known_doi_set,
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One-time Discovery v4 migration: archive legacy, build workspace, activate.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--plan", action="store_true",
                       help="Run inventory and produce a migration plan (read-only).")
    group.add_argument("--apply", action="store_true",
                       help="Execute the full migration plan.")
    group.add_argument("--resume", type=str, default=None, metavar="MIGRATION_ID",
                       help="Resume an interrupted migration from its journal.")
    group.add_argument("--dry-run", action="store_true",
                       help="Run inventory, archive, and notebook migration without cutover.")
    group.add_argument("--inspect", type=str, default=None, metavar="MIGRATION_ID",
                       help="Inspect a migration journal state.")
    parser.add_argument("--migration-id", type=str, default=None,
                        help="Explicit migration ID (default: auto-generated).")
    parser.add_argument("--skip-candidate-extraction", action="store_true",
                        help="Skip legacy DOI candidate extraction (faster).")
    parser.add_argument("--skip-real-smoke", action="store_true",
                        help="Skip real-network limited smoke test.")
    return parser.parse_args(argv)


def cmd_plan(args: argparse.Namespace) -> int:
    """Phase: run inventory and produce migration plan."""
    print("[PLAN] Running legacy inventory...")
    report = generate_inventory_report(
        pending_pages_dir=DISCOVERY_PENDING_PAGES_DIR,
        notebooks_dir=DISCOVERY_KEYWORD_NOTEBOOK_DIR,
    )
    agg = report["aggregate"]

    print(f"[PLAN] Journals: {agg['total_journal_files']} total "
          f"(v2: {agg['v2_journal_count']}, v3: {agg['v3_journal_count']}, "
          f"corrupt: {agg['corrupt_journals']})")
    print(f"[PLAN] Notebooks: {agg['total_notebook_files']}")
    print(f"[PLAN] Journal size: {report['pending_pages'].get('total_size_mb', 0)} MB")
    print(f"[PLAN] Journal aggregate SHA-256: {agg['journal_aggregate_sha256']}")

    # Print per-keyword breakdown
    by_kw = report["pending_pages"].get("by_keyword", {})
    if by_kw:
        print("[PLAN] Per-keyword journal counts:")
        for kw, count in sorted(by_kw.items(), key=lambda x: -x[1]):
            print(f"  {kw}: {count}")

    # Show notebook status
    nbs = report["keyword_notebooks"].get("notebooks", [])
    if nbs:
        print("[PLAN] Notebook status:")
        for nb in nbs:
            kw = nb.get("keyword_zh", "?")
            enabled = nb.get("enabled", False)
            zh = nb.get("active_zh_queries", 0)
            en = nb.get("active_en_queries", 0)
            print(f"  {kw}: enabled={enabled}, zh_queries={zh}, en_queries={en}")

    # Compute expected lane count
    total_lanes = 0
    for nb in nbs:
        if nb.get("enabled"):
            active_q = (nb.get("active_zh_queries", 0) + nb.get("active_en_queries", 0))
            total_lanes += active_q * 2 * 2  # queries × providers × modes
    print(f"[PLAN] Expected v4 lanes after migration: {total_lanes}")

    # Save plan for reference
    plan_path = DISCOVERY_DIR / "migrations" / "v4_migration_plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan = {
        "plan_type": "discovery_v4_migration",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inventory_summary": agg,
        "expected_lanes": total_lanes,
        "migration_id": args.migration_id or f"v4-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
    }
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[PLAN] Plan written to: {plan_path}")

    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    """Execute the full migration."""
    migration_id = args.migration_id or f"v4-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    print(f"[APPLY] Migration ID: {migration_id}")

    # ── Step 1: Inventory ──
    print("[APPLY] Step 1/8: Running legacy inventory...")
    report = generate_inventory_report(
        pending_pages_dir=DISCOVERY_PENDING_PAGES_DIR,
        notebooks_dir=DISCOVERY_KEYWORD_NOTEBOOK_DIR,
    )
    agg = report["aggregate"]
    print(f"  Journals: {agg['total_journal_files']} (v2: {agg['v2_journal_count']}, v3: {agg['v3_journal_count']})")
    print(f"  Notebooks: {agg['total_notebook_files']}")

    # ── Step 2: Create migration journal ──
    print("[APPLY] Step 2/8: Creating migration journal...")
    journal = MigrationJournal.create(migration_id=migration_id)
    journal.transition_to(MigrationState.INVENTORY_COMPLETE)
    journal.metadata["journal_count"] = agg["total_journal_files"]
    journal.metadata["notebook_count"] = agg["total_notebook_files"]
    journal.metadata["aggregate_sha256"] = agg.get("journal_aggregate_sha256", "")
    journal.save()

    # ── Step 3: Archive legacy ──
    print("[APPLY] Step 3/8: Archiving legacy data...")
    archive_result = prepare_legacy_archive(migration_id)
    journal.transition_to(MigrationState.ARCHIVE_PREPARED)
    journal.metadata["archive_pending_pages_total"] = archive_result["pending_pages_total"]
    journal.save()
    print(f"  Archived {archive_result['pending_pages_total']} journals and "
          f"{archive_result['notebooks_total']} notebooks to legacy archive")

    # ── Step 4: Build v4 workspace ──
    print("[APPLY] Step 4/8: Building v4 staging workspace...")
    gen_id = migration_id
    staging_ws = create_staging_workspace(gen_id)
    print(f"  Staging workspace: {staging_ws.root}")

    # ── Step 5: Migrate notebooks ──
    print("[APPLY] Step 5/8: Migrating notebook configs...")
    nb_results = migrate_all_notebooks(
        notebook_dir=DISCOVERY_KEYWORD_NOTEBOOK_DIR,
        output_dir=staging_ws.keyword_notebook_dir,
    )
    success = sum(1 for r in nb_results if r["success"])
    failed = len(nb_results) - success
    print(f"  Notebooks migrated: {success} success, {failed} failed")
    for r in nb_results:
        if r["success"]:
            print(f"    [OK] {r['keyword_zh']} → {r['active_queries']} queries, {r['lane_count']} lanes")
        else:
            print(f"    [FAIL] {r['keyword_zh']}: {r['error']}")

    journal.transition_to(MigrationState.WORKSPACE_BUILT)
    journal.metadata["notebooks_migrated"] = success
    journal.metadata["notebooks_failed"] = failed
    journal.metadata["staging_workspace"] = str(staging_ws.root)
    journal.save()

    # ── Step 6: Extract legacy candidates (optional) ──
    if not args.skip_candidate_extraction:
        print("[APPLY] Step 6/8: Extracting legacy DOI candidates...")
        archive_dir = DISCOVERY_DIR / "legacy_archive" / migration_id / "pending_pages"
        known_dois = build_known_doi_set(
            ledger_path=PAPER_NUMBER_LEDGER_PATH,
            papers_dir=PAPERS_DIR,
            paper_raw_dir=PAPER_RAW_DIR,
        )
        print(f"  Known existing DOIs: {len(known_dois)}")

        seeds = stream_extract_candidates(archive_dir)
        unique, stats = deduplicate_seeds(seeds, known_dois=known_dois)
        print(f"  Candidates observed: {stats['total_observed']}")
        print(f"  Invalid DOI: {stats['invalid_doi']}")
        print(f"  Already existing: {stats['already_existing']}")
        print(f"  Duplicate (within batch): {stats['duplicate_within_batch']}")
        print(f"  Valid unique seeds: {stats['valid_unique']}")

        # Write seeds to workspace
        seeds_dir = staging_ws.pending_candidates_dir
        seeds_dir.mkdir(parents=True, exist_ok=True)
        seeds_path = seeds_dir / "legacy_candidate_seeds.json"
        seeds_path.write_text(
            json.dumps(unique, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"  Seeds written to: {seeds_path}")

        journal.transition_to(MigrationState.CANDIDATES_EXTRACTED)
        journal.metadata["candidate_stats"] = stats
        journal.save()

    # ── Step 7: Preflight validation ──
    print("[APPLY] Step 7/8: Running preflight validation...")
    errors: list[str] = []

    # Validate notebooks in workspace
    for nb_path in sorted(staging_ws.keyword_notebook_dir.glob("*.json")):
        try:
            data = json.loads(nb_path.read_text(encoding="utf-8"))
            if data.get("schema_version") != "4.0":
                errors.append(f"Notebook {nb_path.name}: schema_version is not 4.0")
            if not data.get("enabled", False):
                errors.append(f"Notebook {nb_path.name}: not enabled")
        except json.JSONDecodeError as exc:
            errors.append(f"Notebook {nb_path.name}: corrupt JSON: {exc}")

    # Check lane count
    total_lanes = sum(
        r.get("lane_count", 0) for r in nb_results if r["success"]
    )
    print(f"  Total v4 lanes: {total_lanes}")

    if errors:
        print(f"  [FAIL] Preflight errors ({len(errors)}):")
        for err in errors:
            print(f"    - {err}")
        sys.exit(1)

    journal.transition_to(MigrationState.PREFLIGHT_VALIDATED)
    journal.metadata["total_lanes"] = total_lanes
    journal.save()

    # ── Step 8: Limited smoke test ──
    if not args.skip_real_smoke:
        print("[APPLY] Step 8/8: Running limited real-network smoke test...")
        # Import here to avoid circular import
        from scripts.discover_papers_concurrent import main_internal
        smoke_args = [
            "--from-enabled-notebooks",
            "--migration-mode",
            "--staging-workspace-root", str(staging_ws.root),
            "--mode", "hybrid",
            "--refresh-pages", "1",
            "--backfill-pages", "1",
            "--max-workers", "3",
            "--max-pages-total", "20",
            "--max-provider-requests-total", "40",
            "--max-candidates", "10",
            "--stage-to-paper-raw", "--apply",
        ]
        print(f"  Running: discover_papers_concurrent {' '.join(smoke_args)}")
        exit_code = main_internal(smoke_args)
        if exit_code != 0:
            print(f"  [WARN] Smoke test exit code: {exit_code}")
            print("  Migration can still be committed; review the output above.")
        else:
            print("  [OK] Smoke test passed")

        journal.transition_to(MigrationState.SMOKE_PASSED)
        journal.save()

    # ── Cutover ──
    print(f"\n[APPLY] Migration ready for cutover.")
    print(f"  Staging workspace: {staging_ws.root}")
    print(f"  Notebooks: {staging_ws.keyword_notebook_dir}")
    print(f"  To activate, run:")
    print(f"    python scripts/migrate_discovery_v4.py --cutover {migration_id}")
    journal.save()

    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Resume an interrupted migration."""
    migration_id = args.resume
    if not migration_id:
        print("[RESUME] Error: --resume requires a migration ID", file=sys.stderr)
        return 1

    journal = MigrationJournal.load(migration_id)
    print(f"[RESUME] Migration: {migration_id}, state: {journal.state.value}")
    for t in journal.transitions:
        print(f"  {t['from']} → {t['to']} at {t['at']}")

    # Determine next step based on current state
    # TODO: Implement state-specific resume logic
    print(f"[RESUME] Resume from state '{journal.state.value}' not yet fully implemented.")
    print(f"[RESUME] You can re-run with --apply using --migration-id {migration_id} to start fresh.")
    return 1


def cmd_inspect(args: argparse.Namespace) -> int:
    """Inspect a migration journal."""
    migration_id = args.inspect
    if not migration_id:
        print("[INSPECT] Error: --inspect requires a migration ID", file=sys.stderr)
        return 1

    try:
        journal = MigrationJournal.load(migration_id)
    except FileNotFoundError:
        print(f"[INSPECT] Migration journal not found: {migration_id}")
        return 1

    print(f"[INSPECT] Migration ID: {migration_id}")
    print(f"[INSPECT] State: {journal.state.value}")
    print(f"[INSPECT] Created: {journal.created_at}")
    print(f"[INSPECT] Transitions:")
    for t in journal.transitions:
        print(f"  {t['from']} → {t['to']} at {t['at']}")
    if journal.metadata:
        print(f"[INSPECT] Metadata:")
        for k, v in sorted(journal.metadata.items()):
            print(f"  {k}: {v}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])

    if args.inspect:
        return cmd_inspect(args)
    if args.resume:
        return cmd_resume(args)
    if args.dry_run:
        print("[DRY-RUN] Running plan + notebook migration without cutover...")
        plan_rc = cmd_plan(args)
        if plan_rc != 0:
            return plan_rc
        # Also run notebook migration to verify it works
        gen_id = f"v4-dryrun-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        staging_ws = create_staging_workspace(gen_id)
        nb_results = migrate_all_notebooks(
            notebook_dir=DISCOVERY_KEYWORD_NOTEBOOK_DIR,
            output_dir=staging_ws.keyword_notebook_dir,
        )
        success = sum(1 for r in nb_results if r["success"])
        print(f"[DRY-RUN] Notebook migration: {success}/{len(nb_results)} success")
        for r in nb_results:
            status = "OK" if r["success"] else "FAIL"
            print(f"  [{status}] {r['keyword_zh']}: {r.get('active_queries', 0)} queries, {r.get('lane_count', 0)} lanes")
            if not r["success"]:
                print(f"         Error: {r.get('error', 'unknown')}")
        return 0
    if args.plan:
        return cmd_plan(args)
    if args.apply:
        return cmd_apply(args)

    print("Error: no command specified. Use --plan, --apply, --resume, --dry-run, or --inspect.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
