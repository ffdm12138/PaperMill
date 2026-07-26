#!/usr/bin/env python
"""One-time Discovery v4 migration: archive legacy, build workspace, activate.

Orchestrates the complete 12-phase v4 migration:

    python scripts/migrate_discovery_v4.py --plan
    python scripts/migrate_discovery_v4.py --apply
    python scripts/migrate_discovery_v4.py --resume <migration_id>
    python scripts/migrate_discovery_v4.py --cutover <migration_id>
    python scripts/migrate_discovery_v4.py --post-cutover-validate <migration_id>
    python scripts/migrate_discovery_v4.py --rollback <migration_id>
    python scripts/migrate_discovery_v4.py --clean-legacy <migration_id>
    python scripts/migrate_discovery_v4.py --finalize <migration_id>
    python scripts/migrate_discovery_v4.py --abort <migration_id>
    python scripts/migrate_discovery_v4.py --dry-run

Migration states: planned → inventory_complete → archive_prepared →
workspace_built → notebooks_staged → candidates_extracted →
preflight_validated → smoke_failed (recoverable) → smoke_passed →
cutover_committed → legacy_cleaned → finalized

ABORTED is a terminal state reachable from any pre-cutover state, and from
cutover_committed via --rollback (the only post-cutover escape).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (
    DISCOVERY_DIR,
    DISCOVERY_KEYWORD_NOTEBOOK_DIR,
    DISCOVERY_LEGACY_ARCHIVE_DIR,
    DISCOVERY_MIGRATIONS_DIR,
    DISCOVERY_PENDING_PAGES_DIR,
    PAPER_NUMBER_LEDGER_PATH,
    PAPER_RAW_DIR,
    PAPERS_DIR,
)
from src.discovery.contracts.candidate import PendingCandidateV4
from src.discovery.contracts.notebook import (
    validate_discovery_readiness,
    validate_notebook,
)
from src.discovery.contracts.manifest import (
    ActiveGenerationPointerV4,
    DiscoveryWorkspaceManifestV4,
)
from src.discovery.stores.pending_candidate_store import PendingCandidateStoreV4
from src.discovery.workspace import (
    ACTIVE_GENERATION_PATH,
    DISCOVERY_GENERATIONS_DIR,
    CutoverLockError,
    CutoverReconciliationError,
    DiscoveryWorkspace,
    STAGING_DIR,
    WorkspaceResolver,
    create_staging_workspace,
    commit_workspace,
    hash_workspace_tree,
)
from src.migrations.discovery_v4.legacy_inventory import generate_inventory_report
from src.migrations.discovery_v4 import migration_journal as _journal_mod
from src.discovery.maintenance_gate import (
    MigrationMaintenanceLock,
    MigrationMaintenanceLockError,
)
from src.migrations.discovery_v4.migration_journal import MigrationJournal, MigrationState
from src.migrations.discovery_v4.archive_builder import prepare_legacy_archive
from src.migrations.discovery_v4.notebook_migration import migrate_all_notebooks
from src.migrations.discovery_v4.candidate_extraction import (
    CandidateConservationError,
    CandidateExtractionReport,
    SqliteDoiIndex,
    assert_conservation,
    stream_extract_candidates,
    build_known_doi_index,
)
from src.migrations.discovery_v4.legacy_contracts.candidate import (
    LegacyCandidateSeedV4,
)
from src.migrations.discovery_v4.legacy_contracts.page_journal_v3 import (
    LegacyPageJournalContractError,
)
from src.migrations.discovery_v4.legacy_retirement import (
    LegacyRetirementError,
    purge_retained_legacy,
    retire_legacy_sources,
)


# ── Hash helpers ──────────────────────────────────────────────────────────


class MigrationStepError(RuntimeError):
    """A migration apply step failed; the journal is not advanced."""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_directory(root: Path, exclude: set[str] | None = None) -> str:
    """Deterministic hash of every file under root as ``path:sha256`` lines.

    Delegates to ``src.discovery.workspace.hash_workspace_tree`` so the
    manifest build and ``resolve_active(verify_tree=True)`` always agree on
    tree-hash semantics.
    """
    return hash_workspace_tree(root, exclude=exclude)


def _atomic_write_json(path: Path, data: dict[str, object]) -> None:
    """Atomic JSON write (tmp + fsync + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        with tmp.open("wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(path))
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _build_workspace_manifest(
    staging_ws: DiscoveryWorkspace,
    migration_id: str,
    inventory_sha256: str | None,
    total_lanes: int,
) -> tuple[DiscoveryWorkspaceManifestV4, str]:
    """Write workspace.json to the staging workspace and return its hash.

    Must be called at cutover time, after the smoke run: all set hashes,
    counts, and ``workspace_tree_sha256`` are computed live from the settled
    workspace content (never empty placeholders).
    """
    now = datetime.now(timezone.utc).isoformat()
    nb_files = sorted(staging_ws.keyword_notebook_dir.glob("*.json"))
    notebook_count = len(nb_files)

    nb_hashes: list[str] = []
    query_count = 0
    relevance_profile_hashes: list[str] = []
    for nb_path in nb_files:
        nb_hashes.append(_sha256_file(nb_path))
        data = json.loads(nb_path.read_text(encoding="utf-8"))
        queries = data.get("search_queries", {})
        query_count += sum(
            1 for q in queries.values()
            if isinstance(q, dict) and q.get("active")
        )
        rp = data.get("relevance_profile")
        if isinstance(rp, dict):
            relevance_profile_hashes.append(str(rp.get("profile_hash", "")))

    notebook_set_hash = _sha256_text("\n".join(sorted(nb_hashes)))
    relevance_profile_hash = _sha256_text("\n".join(sorted(relevance_profile_hashes)))

    pending_store = PendingCandidateStoreV4(staging_ws)
    pending_candidate_count = pending_store.count()
    pending_dir = staging_ws.pending_candidates_dir
    if pending_dir.is_dir():
        pending_set_hash = _hash_directory(pending_dir)
    else:
        pending_set_hash = _sha256_text("")

    # The smoke run is side-effect free: it executes against an ephemeral
    # clone of the staging workspace and never writes here.  The manifest
    # built at cutover time therefore reflects the migration artifacts
    # themselves, computed live from the settled workspace content instead
    # of empty placeholders.
    lane_states_dir = staging_ws.lane_states_dir
    if lane_states_dir.is_dir():
        lane_state_set_hash = _hash_directory(lane_states_dir)
    else:
        lane_state_set_hash = _sha256_text("")
    page_journals_dir = staging_ws.page_journals_dir
    if page_journals_dir.is_dir():
        page_journal_files = [
            p for p in page_journals_dir.rglob("*.json") if p.is_file()
        ]
        page_journal_set_hash = _hash_directory(page_journals_dir)
    else:
        page_journal_files = []
        page_journal_set_hash = _sha256_text("")

    manifest = DiscoveryWorkspaceManifestV4(
        schema_version="4.0",
        generation_id=staging_ws.generation_id,
        migration_id=migration_id,
        created_at=now,
        completed_at=now,
        notebook_count=notebook_count,
        query_count=query_count,
        lane_count=total_lanes,
        page_journal_count=len(page_journal_files),
        pending_candidate_count=pending_candidate_count,
        notebook_set_hash=notebook_set_hash,
        lane_state_set_hash=lane_state_set_hash,
        page_journal_set_hash=page_journal_set_hash,
        pending_set_hash=pending_set_hash,
        relevance_profile_hash=relevance_profile_hash,
        store_schema_versions={"keyword_notebook": "4.0"},
        workspace_tree_sha256=_hash_directory(staging_ws.root, exclude={"workspace.json"}),
        migration_inventory_sha256=inventory_sha256 or "",
    )

    manifest_path = staging_ws.root / "workspace.json"
    _atomic_write_json(manifest_path, manifest.to_dict())
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    return manifest, manifest_sha256


def _apply_dir_overrides(args: argparse.Namespace) -> None:
    """Apply --migrations-dir and --staging-dir overrides in-process.

    This is a best-effort override for testability.  It patches the module-level
    constants used by the journal and workspace helpers; other discovery paths
    (active_generation.json, generations/) are still governed by config.settings.
    """
    import config.settings as settings
    import src.discovery.workspace as workspace_mod
    import src.migrations.discovery_v4.migration_journal as journal_mod

    if args.migrations_dir:
        path = Path(args.migrations_dir)
        path.mkdir(parents=True, exist_ok=True)
        settings.DISCOVERY_MIGRATIONS_DIR = path
        journal_mod.DISCOVERY_MIGRATIONS_DIR = path

    if args.staging_dir:
        path = Path(args.staging_dir)
        path.mkdir(parents=True, exist_ok=True)
        settings.DISCOVERY_STAGING_DIR = path
        workspace_mod.STAGING_DIR = path


# ── CLI parser ────────────────────────────────────────────────────────────


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
    group.add_argument("--cutover", type=str, default=None, metavar="MIGRATION_ID",
                       help="Commit a staging workspace as the active v4 generation.")
    group.add_argument("--post-cutover-validate", type=str, default=None, metavar="MIGRATION_ID",
                       help="Read-only validation of the active workspace after cutover.")
    group.add_argument("--rollback", type=str, default=None, metavar="MIGRATION_ID",
                       help="Roll back a committed cutover (only from cutover_committed).")
    group.add_argument("--clean-legacy", type=str, default=None, metavar="MIGRATION_ID",
                       help="Validate the active workspace, then delete the legacy archive.")
    group.add_argument("--finalize", type=str, default=None, metavar="MIGRATION_ID",
                       help="Write finalize evidence and close the migration.")
    group.add_argument("--retire-legacy-sources", type=str, default=None, metavar="MIGRATION_ID",
                       help="Retire the flat legacy dirs into legacy_retained/<id> "
                            "after reconciliation is clean (journal must be finalized).")
    group.add_argument("--purge-retained-legacy", type=str, default=None, metavar="MIGRATION_ID",
                       help="Purge the retained legacy tree after its retention window "
                            "(requires --confirm-migration-id).")
    group.add_argument("--abort", type=str, default=None, metavar="MIGRATION_ID",
                       help="Abort a migration and optionally clean up its staging workspace.")
    group.add_argument("--dry-run", action="store_true",
                       help="Run inventory and validate notebook migration without writes.")
    group.add_argument("--inspect", type=str, default=None, metavar="MIGRATION_ID",
                       help="Inspect a migration journal state.")
    parser.add_argument("--migration-id", type=str, default=None,
                        help="Explicit migration ID (default: auto-generated).")
    parser.add_argument("--skip-real-smoke", action="store_true",
                        help="Skip real-network limited smoke test.")
    parser.add_argument("--migrations-dir", type=Path, default=None,
                        help="Override migration journal directory (tests).")
    parser.add_argument("--staging-dir", type=Path, default=None,
                        help="Override staging workspace directory (tests).")
    parser.add_argument("--plan-output", type=Path, default=None,
                        help="Optionally write the --plan/--dry-run plan JSON to this "
                             "path (atomic write).  Default: print only, write nothing.")
    parser.add_argument("--confirm-migration-id", type=str, default=None,
                        help="Required with --purge-retained-legacy; must equal the "
                             "migration ID being purged.")
    parser.add_argument("--retention-days", type=int, default=90,
                        help="Retention window in days for --retire-legacy-sources "
                             "(default: 90).")
    return parser.parse_args(argv)


# ── Plan ──────────────────────────────────────────────────────────────────


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

    # Read-only by default: the plan is printed to stdout and nothing is
    # written unless the operator explicitly passes --plan-output.
    plan = {
        "plan_type": "discovery_v4_migration",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inventory_summary": agg,
        "expected_lanes": total_lanes,
        "migration_id": args.migration_id or f"v4-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if args.plan_output is not None:
        _atomic_write_json(Path(args.plan_output), plan)
        print(f"[PLAN] Plan written to: {args.plan_output}")

    return 0


# ── Apply step helpers ────────────────────────────────────────────────────


def _binding_path(raw_path: object) -> str:
    """Normalize a manifest/inventory relative path to posix separators.

    ``str(Path.relative_to(...))`` yields backslashes on Windows; both the
    inventory and the archive manifests must hash identical lines.
    """
    return PureWindowsPath(str(raw_path)).as_posix()


def _inventory_binding_lines(report: dict[str, object]) -> list[str]:
    """Sorted ``path:sha256:size`` lines for every hashed inventory entry.

    Entries without a recorded hash/size (e.g. mocked reports in tests)
    cannot be bound and are skipped; the real inventory always records both
    for every file.
    """
    lines: list[str] = []
    pages = report.get("pending_pages", {})
    if isinstance(pages, dict):
        for entry in pages.get("files", []) or []:
            if not isinstance(entry, dict):
                continue
            sha, size = entry.get("sha256"), entry.get("size")
            if sha and isinstance(size, int):
                lines.append(f"{_binding_path(entry.get('path'))}:{sha}:{size}")
    notebooks = report.get("keyword_notebooks", {})
    if isinstance(notebooks, dict):
        for entry in notebooks.get("notebooks", []) or []:
            if not isinstance(entry, dict):
                continue
            sha, size = entry.get("sha256"), entry.get("size")
            if sha and isinstance(size, int):
                lines.append(f"{_binding_path(entry.get('path'))}:{sha}:{size}")
    return sorted(lines)


def _binding_sha256(lines: list[str]) -> str:
    return _sha256_text("\n".join(lines))


def _read_archive_manifest(archive_root: Path, section: str) -> dict[str, object]:
    manifest_path = archive_root / section / "archive_manifest.json"
    if not manifest_path.is_file():
        raise MigrationStepError(f"archive manifest missing: {manifest_path}")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise MigrationStepError(
            f"cannot read archive manifest {manifest_path}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise MigrationStepError(f"archive manifest is not a JSON object: {manifest_path}")
    return data


def _archive_binding_lines(archive_root: Path) -> list[str]:
    """Sorted ``path:sha256:size`` lines from both archive manifests.

    Notebook archive entries store only the file name; they are normalized
    to the inventory's ``keyword_notebooks/<name>`` convention so both sides
    hash identical lines.
    """
    lines: list[str] = []
    pages_manifest = _read_archive_manifest(archive_root, "pending_pages")
    for entry in pages_manifest.get("files", []) or []:
        lines.append(
            f"{_binding_path(entry['path'])}:{entry['sha256']}:{entry['size']}"
        )
    nb_manifest = _read_archive_manifest(archive_root, "keyword_notebooks")
    for entry in nb_manifest.get("files", []) or []:
        lines.append(
            f"{_binding_path('keyword_notebooks/' + str(entry['path']))}"
            f":{entry['sha256']}:{entry['size']}"
        )
    return sorted(lines)


def _archive_binding_matches(journal: MigrationJournal, archive_root: Path) -> bool:
    """True when the archive content matches the inventory binding closure.

    Journals created before the binding existed carry no
    ``inventory_binding_sha256``; the check is skipped for them.
    """
    expected = journal.metadata.get("inventory_binding_sha256")
    if not expected:
        return True
    try:
        actual = _binding_sha256(_archive_binding_lines(archive_root))
    except MigrationStepError:
        return False
    return actual == expected


def _assert_archive_binding(journal: MigrationJournal, archive_root: Path) -> None:
    """Fail closed unless the archived bytes match the inventory closure.

    The archive step runs after the inventory step; any source file changed
    in between makes the archived copy diverge from the inventoried bytes.
    """
    expected = journal.metadata.get("inventory_binding_sha256")
    if not expected:
        print("  [SKIP] journal has no inventory binding hash; "
              "archive/inventory binding not enforced")
        return
    actual = _binding_sha256(_archive_binding_lines(archive_root))
    if actual != expected:
        raise MigrationStepError(
            "archive content does not match the inventory closure: source "
            "files changed between the inventory and archive steps. "
            "Abort this migration (--abort) and restart it so the inventory "
            "is re-taken."
        )
    print("  Archive bound to inventory closure: aggregate hash matches")


def _archive_result_from_manifests(
    archive_root: Path, migration_id: str
) -> dict[str, object]:
    """Rebuild the archive-step result dict from on-disk manifests (resume)."""
    pages_manifest = _read_archive_manifest(archive_root, "pending_pages")
    nb_manifest = _read_archive_manifest(archive_root, "keyword_notebooks")
    return {
        "migration_id": migration_id,
        "archive_root": str(archive_root),
        "pending_pages": pages_manifest,
        "keyword_notebooks": nb_manifest,
        "pending_pages_total": pages_manifest.get("total_files", 0),
        "notebooks_total": nb_manifest.get("total_files", 0),
    }


def _workspace_at_root(root: Path) -> DiscoveryWorkspace:
    """Return a DiscoveryWorkspace for an existing generation/staging root."""
    return DiscoveryWorkspace(
        generation_id=root.name,
        root=root,
        keyword_notebook_dir=root / "keyword_notebooks",
        lane_states_dir=root / "lane_states",
        page_journals_dir=root / "page_journals",
        pending_candidates_dir=root / "pending_candidates",
        indexes_dir=root / "indexes",
        exports_dir=root / "exports",
        reports_dir=root / "reports",
        locks_dir=root / "locks",
    )


def _resolve_staging_ws_from_journal(journal: MigrationJournal) -> DiscoveryWorkspace:
    """Return a DiscoveryWorkspace for the staging root stored in the journal."""
    root_str = journal.metadata.get("staging_workspace")
    if not root_str:
        raise RuntimeError("journal metadata missing 'staging_workspace'")
    root = Path(root_str)
    if not root.is_dir():
        raise RuntimeError(f"staging workspace does not exist: {root}")
    return _workspace_at_root(root)


def _step_inventory(journal: MigrationJournal, args: argparse.Namespace) -> dict[str, object]:
    print("[APPLY] Step 1/8: Running legacy inventory...")
    report = generate_inventory_report(
        pending_pages_dir=DISCOVERY_PENDING_PAGES_DIR,
        notebooks_dir=DISCOVERY_KEYWORD_NOTEBOOK_DIR,
    )
    agg = report["aggregate"]
    print(f"  Journals: {agg['total_journal_files']} (v2: {agg['v2_journal_count']}, v3: {agg['v3_journal_count']})")
    print(f"  Notebooks: {agg['total_notebook_files']}")
    journal.metadata["journal_count"] = agg["total_journal_files"]
    journal.metadata["notebook_count"] = agg["total_notebook_files"]
    journal.metadata["aggregate_sha256"] = agg.get("journal_aggregate_sha256", "")
    # Eligible legacy candidates drive the automatic candidate-extraction
    # skip: only a proven-zero inventory may bypass the extraction step.
    eligible = sum(
        int(entry.get("candidate_count", 0))
        for entry in report["pending_pages"].get("files", [])
        if isinstance(entry, dict) and "error" not in entry
    )
    journal.metadata["eligible_legacy_candidates"] = eligible
    # Inventory-derived expectations used by the notebook/preflight gates.
    enabled_nbs = [
        nb for nb in report["keyword_notebooks"].get("notebooks", [])
        if nb.get("enabled")
    ]
    journal.metadata["inventory_enabled_notebook_count"] = len(enabled_nbs)
    journal.metadata["inventory_enabled_keyword_zh"] = sorted(
        str(nb.get("keyword_zh") or "") for nb in enabled_nbs
    )
    # Binding closure for the archive step: every inventoried source file's
    # path + sha256 + size, aggregated.  The archive step must reproduce
    # exactly these bytes or fail closed.
    binding_lines = _inventory_binding_lines(report)
    journal.metadata["inventory_binding_sha256"] = _binding_sha256(binding_lines)
    journal.metadata["inventory_bound_files"] = len(binding_lines)
    journal.transition_to(MigrationState.INVENTORY_COMPLETE)
    journal.save()
    return report


def _step_archive(journal: MigrationJournal, args: argparse.Namespace) -> dict[str, object]:
    print("[APPLY] Step 2/8: Archiving legacy data...")
    archive_root = DISCOVERY_LEGACY_ARCHIVE_DIR / journal.migration_id
    archive_result: dict[str, object] | None = None
    if archive_root.exists():
        # Crash reconcile: the journal never advanced past the inventory, so
        # any existing archive comes from a crashed earlier attempt.  Reuse
        # it only when it verifies byte-for-byte AND matches the inventory
        # binding closure; otherwise rebuild it from the live sources.
        errors = _verify_legacy_archive(archive_root)
        if not errors and _archive_binding_matches(journal, archive_root):
            print(f"  Reusing verified archive from previous attempt: {archive_root}")
            archive_result = _archive_result_from_manifests(
                archive_root, journal.migration_id
            )
        else:
            print(f"  Discarding invalid leftover archive ({len(errors)} "
                  "verification error(s) or inventory binding mismatch); rebuilding")
            shutil.rmtree(archive_root)
    if archive_result is None:
        archive_result = prepare_legacy_archive(journal.migration_id)
    _assert_archive_binding(journal, archive_root)
    journal.metadata["archive_pending_pages_total"] = archive_result["pending_pages_total"]
    journal.transition_to(MigrationState.ARCHIVE_PREPARED)
    journal.save()
    print(f"  Archived {archive_result['pending_pages_total']} journals and "
          f"{archive_result['notebooks_total']} notebooks to legacy archive")
    return archive_result


def _step_build_workspace(journal: MigrationJournal, args: argparse.Namespace) -> DiscoveryWorkspace:
    print("[APPLY] Step 3/8: Building v4 staging workspace...")
    existing_root = STAGING_DIR / journal.migration_id
    if existing_root.is_dir():
        # Crash reconcile: a previous attempt created the staging root but
        # died before the journal advanced; adopt it instead of failing on
        # FileExistsError.
        staging_ws = _workspace_at_root(existing_root)
        staging_ws.ensure_dirs()
        print(f"  Reusing existing staging workspace: {staging_ws.root}")
    else:
        staging_ws = create_staging_workspace(journal.migration_id)
        print(f"  Staging workspace: {staging_ws.root}")
    journal.metadata["staging_workspace"] = str(staging_ws.root)
    journal.transition_to(MigrationState.WORKSPACE_BUILT)
    journal.save()
    return staging_ws


def _step_migrate_notebooks(
    journal: MigrationJournal,
    staging_ws: DiscoveryWorkspace,
    args: argparse.Namespace,
) -> list[dict[str, object]]:
    print("[APPLY] Step 4/8: Migrating notebook configs...")
    # Read from the legacy archive snapshot produced by the archive step
    # (step 2), not the live notebook directory: inventory, archive, and
    # notebook migration must all observe the same point-in-time bytes.
    snapshot_dir = (
        DISCOVERY_LEGACY_ARCHIVE_DIR / journal.migration_id / "keyword_notebooks"
    )
    if not snapshot_dir.is_dir():
        raise MigrationStepError(
            f"legacy archive notebook snapshot missing: {snapshot_dir}; "
            "the archive step must complete before notebook migration"
        )
    nb_results = migrate_all_notebooks(
        notebook_dir=snapshot_dir,
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
    journal.metadata["notebooks_migrated"] = success
    journal.metadata["notebooks_failed"] = failed
    if failed:
        raise MigrationStepError(
            f"notebook migration failed for {failed} notebook(s); "
            "fix the source notebooks and resume the migration"
        )
    expected_zh = journal.metadata.get("inventory_enabled_keyword_zh")
    if not isinstance(expected_zh, list):
        raise MigrationStepError(
            "journal metadata missing 'inventory_enabled_keyword_zh'; "
            "cannot verify migrated notebooks against the inventory"
        )
    if success != len(expected_zh):
        raise MigrationStepError(
            f"migrated notebook count {success} does not match "
            f"inventory enabled notebook count {len(expected_zh)}"
        )
    journal.transition_to(MigrationState.NOTEBOOKS_STAGED)
    journal.save()
    return nb_results


def _staged_keyword_identity(
    staging_ws: DiscoveryWorkspace,
) -> tuple[set[str], dict[str, str]]:
    """Return (keyword_ids, keyword_zh -> keyword_id) for staged v4 notebooks."""
    ids: set[str] = set()
    by_zh: dict[str, str] = {}
    for nb_path in sorted(staging_ws.keyword_notebook_dir.glob("*.json")):
        try:
            data = json.loads(nb_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        kid = str(data.get("keyword_id") or "")
        kzh = str(data.get("keyword_zh") or "")
        if kid:
            ids.add(kid)
        if kid and kzh:
            by_zh.setdefault(kzh, kid)
    return ids, by_zh


def _legacy_seed_to_pending_candidate(
    seed: LegacyCandidateSeedV4,
    keyword_id_value: str,
    *,
    created_at: str,
) -> PendingCandidateV4:
    """Build a strict v4 pending candidate from one extracted legacy seed."""
    candidate_id = seed.legacy_candidate_id.strip()
    if not candidate_id:
        candidate_id = f"legacy-{_sha256_text(seed.normalized_doi)[:16]}"
    raw_provider_data = {
        "provider": seed.provider,
        "lane": seed.lane,
        "query_id": seed.query_id,
        "query": seed.query,
        "keyword_zh": seed.keyword_zh,
        "legacy_keyword_id": seed.keyword_id,
        "source_schema_version": seed.source_schema_version,
        "seed_id": seed.seed_id,
    }
    return PendingCandidateV4(
        candidate_id=candidate_id,
        keyword_id=keyword_id_value,
        origin="legacy_candidate_seed",
        source_page_id=seed.legacy_page_id or None,
        doi=seed.doi or None,
        normalized_doi=seed.normalized_doi or None,
        title=seed.title,
        authors=list(seed.authors) if seed.authors is not None else None,
        year=seed.year,
        venue=seed.venue,
        raw_provider_data=raw_provider_data,
        created_at=created_at,
    )


def _quarantine_path(migration_id: str) -> Path:
    """Strict quarantine file for unresolved legacy candidates."""
    return DISCOVERY_MIGRATIONS_DIR / f"{migration_id}.candidate_quarantine.jsonl"


def _step_extract_candidates(
    journal: MigrationJournal,
    staging_ws: DiscoveryWorkspace,
    args: argparse.Namespace,
) -> CandidateExtractionReport:
    print("[APPLY] Step 5/8: Extracting legacy DOI candidates...")
    archive_dir = DISCOVERY_DIR / "legacy_archive" / journal.migration_id / "pending_pages"

    extraction_stats: dict[str, int] = {}
    staged_ids, staged_ids_by_zh = _staged_keyword_identity(staging_ws)
    # Candidate created_at is bound to the migration journal, not wall
    # clock: a resume must reproduce byte-identical pending candidates so
    # the store's create-if-absent write is a true idempotent hit.
    created_at = journal.created_at
    pending_store = PendingCandidateStoreV4(staging_ws)
    errors: list[str] = []
    imported = 0
    quarantined = 0

    # Streaming quarantine: unresolved records are appended one JSONL line
    # at a time and atomically published at the end — a write failure is a
    # hard gate that blocks the journal transition.
    quarantine_path = _quarantine_path(journal.migration_id)
    quarantine_tmp = quarantine_path.with_suffix(quarantine_path.suffix + ".tmp")
    quarantine_fh = None

    def _quarantine_record(record: dict[str, object]) -> None:
        nonlocal quarantine_fh
        if quarantine_fh is None:
            quarantine_path.parent.mkdir(parents=True, exist_ok=True)
            quarantine_fh = quarantine_tmp.open("wb")
        line = json.dumps(record, ensure_ascii=False).encode("utf-8")
        quarantine_fh.write(line + b"\n")

    try:
        with tempfile.TemporaryDirectory(prefix="mineru_v4_doi_index_") as index_dir:
            known_count = build_known_doi_index(
                ledger_path=PAPER_NUMBER_LEDGER_PATH,
                papers_dir=PAPERS_DIR,
                paper_raw_dir=PAPER_RAW_DIR,
                db_path=Path(index_dir) / "known_dois.sqlite",
            )
            print(f"  Known existing DOIs: {known_count}")
            with SqliteDoiIndex(Path(index_dir) / "known_dois.sqlite") as known_index, \
                    SqliteDoiIndex(Path(index_dir) / "batch_dois.sqlite") as batch_index:
                try:
                    # Fully streaming: one journal at a time, seeds are
                    # attributed and persisted immediately via the store's
                    # create-if-absent write; no candidate lists accumulate
                    # in memory.  A resume rewrites identical payloads
                    # idempotently; a genuine identity collision raises.
                    for seed in stream_extract_candidates(
                        archive_dir,
                        known_doi_index=known_index,
                        batch_index=batch_index,
                        stats=extraction_stats,
                    ):
                        keyword_id_value = seed.keyword_id
                        if keyword_id_value not in staged_ids:
                            keyword_id_value = staged_ids_by_zh.get(seed.keyword_zh, "")
                        if not keyword_id_value:
                            reason = (
                                "keyword attribution unresolved: no staged v4 "
                                f"notebook for legacy_keyword_id={seed.keyword_id!r} "
                                f"keyword_zh={seed.keyword_zh!r}"
                            )
                            errors.append(
                                f"keyword attribution unresolved: doi={seed.normalized_doi} "
                                f"legacy_keyword_id={seed.keyword_id!r} "
                                f"keyword_zh={seed.keyword_zh!r}"
                            )
                            _quarantine_record({
                                "reason": reason,
                                "seed": seed.to_dict(),
                            })
                            quarantined += 1
                            continue
                        pending_store.write(
                            _legacy_seed_to_pending_candidate(
                                seed, keyword_id_value, created_at=created_at
                            )
                        )
                        imported += 1
                except LegacyPageJournalContractError as exc:
                    raise MigrationStepError(
                        f"legacy page journal failed strict validation: {exc}"
                    ) from exc
    finally:
        if quarantine_fh is not None:
            quarantine_fh.flush()
            os.fsync(quarantine_fh.fileno())
            quarantine_fh.close()
            os.replace(str(quarantine_tmp), str(quarantine_path))
        elif quarantine_tmp.exists():
            quarantine_tmp.unlink()

    print(f"  Candidates observed: {extraction_stats.get('candidates_observed', 0)}")
    print(f"  Invalid DOI: {extraction_stats.get('invalid_doi', 0)}")
    print(f"  Already existing: {extraction_stats.get('already_existing', 0)}")
    print(f"  Duplicate (within batch): {extraction_stats.get('duplicate_seeds', 0)}")
    print(f"  Terminal (not re-ingested): {extraction_stats.get('terminal', 0)}")
    print(f"  Valid unique seeds: {extraction_stats.get('valid_doi_seeds', 0)}")
    if quarantined:
        print(f"  Quarantined unresolved candidates: {quarantined} "
              f"-> {quarantine_path}")

    report = CandidateExtractionReport(
        journals_scanned=extraction_stats.get("journals_scanned", 0),
        candidates_observed=extraction_stats.get("candidates_observed", 0),
        valid_doi_seeds=extraction_stats.get("valid_doi_seeds", 0),
        invalid_doi=extraction_stats.get("invalid_doi", 0),
        already_existing=extraction_stats.get("already_existing", 0),
        duplicate_seeds=extraction_stats.get("duplicate_seeds", 0),
        imported=imported,
        terminal=extraction_stats.get("terminal", 0),
        quarantined=quarantined,
        unresolved=0,
        errors=errors,
    )

    # Hard gate: conservation — every observed candidate is accounted for.
    try:
        assert_conservation(report)
    except CandidateConservationError as exc:
        raise MigrationStepError(str(exc)) from exc

    print(f"  Pending candidates written: {report.imported} "
          f"({report.quarantined} quarantined unresolved keyword attribution)")

    journal.metadata["candidate_stats"] = {
        "journals_scanned": report.journals_scanned,
        "candidates_observed": report.candidates_observed,
        "valid_doi_seeds": report.valid_doi_seeds,
        "invalid_doi": report.invalid_doi,
        "already_existing": report.already_existing,
        "duplicate_seeds": report.duplicate_seeds,
        "imported": report.imported,
        "terminal": report.terminal,
        "quarantined": report.quarantined,
        "unresolved": report.unresolved,
        "errors": list(report.errors),
    }
    journal.transition_to(MigrationState.CANDIDATES_EXTRACTED)
    journal.save()
    return report


def _step_preflight(
    journal: MigrationJournal,
    staging_ws: DiscoveryWorkspace,
    nb_results: list[dict[str, object]],
    args: argparse.Namespace,
) -> None:
    print("[APPLY] Step 6/8: Running preflight validation...")
    errors: list[str] = []

    # The notebook migration step must have completed without failures.
    notebooks_failed = journal.metadata.get("notebooks_failed", 0)
    if notebooks_failed:
        errors.append(
            f"journal metadata reports notebooks_failed={notebooks_failed}"
        )

    # Inventory-derived expectations, read from the journal (never hard-coded).
    expected_count = journal.metadata.get("inventory_enabled_notebook_count")
    expected_zh = journal.metadata.get("inventory_enabled_keyword_zh")
    if not isinstance(expected_count, int) or not isinstance(expected_zh, list):
        errors.append(
            "journal metadata missing inventory enabled-notebook expectations "
            "(inventory_enabled_notebook_count / inventory_enabled_keyword_zh)"
        )
        expected_count = None
        expected_zh = None

    # Validate notebooks in workspace
    staged_v4_count = 0
    staged_zh: list[str] = []
    for nb_path in sorted(staging_ws.keyword_notebook_dir.glob("*.json")):
        try:
            data = json.loads(nb_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            errors.append(f"Notebook {nb_path.name}: unreadable: {exc}")
            continue
        if not isinstance(data, dict):
            errors.append(f"Notebook {nb_path.name}: not a JSON object")
            continue
        if data.get("schema_version") != "4.0":
            errors.append(f"Notebook {nb_path.name}: schema_version is not 4.0")
            continue
        staged_v4_count += 1
        staged_zh.append(str(data.get("keyword_zh") or ""))
        if not data.get("enabled", False):
            errors.append(f"Notebook {nb_path.name}: not enabled")
        try:
            validate_notebook(data)
        except Exception as exc:
            errors.append(f"Notebook {nb_path.name}: invalid v4 notebook: {exc}")
            continue
        readiness = validate_discovery_readiness(data)
        if not readiness.ready:
            errors.append(
                f"Notebook {nb_path.name}: not bilingual discovery-ready: "
                + "; ".join(readiness.errors)
            )

    # The staged set must exactly match the inventory enabled set.
    if expected_count is not None and staged_v4_count != expected_count:
        errors.append(
            f"staged v4 notebook count {staged_v4_count} does not match "
            f"inventory enabled count {expected_count}"
        )
    if expected_zh is not None and set(staged_zh) != {str(k) for k in expected_zh}:
        errors.append(
            f"staged keyword_zh set {sorted(staged_zh)} does not match "
            f"inventory enabled set {sorted(str(k) for k in expected_zh)}"
        )

    # Check lane count
    total_lanes = sum(
        r.get("lane_count", 0) for r in nb_results if r["success"]
    )
    print(f"  Total v4 lanes: {total_lanes}")
    if total_lanes <= 0:
        errors.append("total v4 lanes is 0; discovery would have nothing to run")

    # Candidate migration integrity: nothing unresolved, nothing
    # quarantined, exact conservation, and the pending store must hold
    # exactly the imported candidates.  A quarantine blocks cutover until
    # an operator resolves it — there is deliberately no silent path past
    # it.
    stats = journal.metadata.get("candidate_stats")
    if not isinstance(stats, dict):
        errors.append("journal metadata missing candidate_stats")
    else:
        unresolved = int(stats.get("unresolved", -1))
        quarantined = int(stats.get("quarantined", -1))
        observed = int(stats.get("candidates_observed", -1))
        imported = int(stats.get("imported", -1))
        accounted = (
            int(stats.get("invalid_doi", 0))
            + int(stats.get("already_existing", 0))
            + int(stats.get("duplicate_seeds", 0))
            + imported
            + int(stats.get("terminal", 0))
            + quarantined
            + unresolved
        )
        if unresolved != 0:
            errors.append(f"candidate_stats.unresolved={unresolved} must be 0")
        if quarantined != 0:
            errors.append(
                f"candidate_stats.quarantined={quarantined} must be 0: "
                "quarantined candidates block cutover until resolved"
            )
        if accounted != observed:
            errors.append(
                f"candidate conservation error: observed={observed} but "
                f"accounted={accounted}"
            )
        pending_count = PendingCandidateStoreV4(staging_ws).count()
        if pending_count != imported:
            errors.append(
                f"pending store holds {pending_count} candidate(s) but "
                f"candidate_stats.imported={imported}"
            )

    if errors:
        print(f"  [FAIL] Preflight errors ({len(errors)}):")
        for err in errors:
            print(f"    - {err}")
        raise MigrationStepError(
            f"preflight validation failed with {len(errors)} error(s)"
        )

    journal.transition_to(MigrationState.PREFLIGHT_VALIDATED)
    journal.metadata["total_lanes"] = total_lanes
    journal.save()


def _clone_workspace_for_smoke(
    staging_ws: DiscoveryWorkspace,
    clone_root: Path,
) -> DiscoveryWorkspace:
    """Copy the staging workspace into an ephemeral clone for the smoke run.

    The smoke run mutates the clone freely (lane states, page journals,
    pending-candidate drains); the real staging workspace must stay
    byte-identical.  Lock files are stripped from the clone so live
    workspace locks cannot deadlock or interfere with the smoke run.
    """
    shutil.copytree(staging_ws.root, clone_root)
    for lock_file in sorted(clone_root.rglob("*.lock")):
        lock_file.unlink()
    return _workspace_at_root(clone_root)


def _step_smoke(
    journal: MigrationJournal,
    staging_ws: DiscoveryWorkspace,
    args: argparse.Namespace,
) -> int:
    print("[APPLY] Step 7/8: Running limited real-network smoke test...")
    if args.skip_real_smoke:
        print("  [SKIP] --skip-real-smoke set; real-network smoke not run")
        journal.metadata["smoke_skipped"] = True
        journal.metadata["smoke_skip_reason"] = "--skip-real-smoke flag"
        journal.save()
        print("  Migration stays before smoke_passed; cutover requires a real smoke pass.")
        return 0

    # Zero-side-effect contract: the smoke run executes against an ephemeral
    # full clone of the staging workspace.  The real staging workspace is
    # hashed before and after the run; any drift fails the smoke step and
    # blocks cutover, because smoke writes (lane states, page journals,
    # drained pending candidates) would otherwise silently consume migration
    # artifacts.  Global side channels (batch reports, DOI candidate exports,
    # paper_raw, the ledger) are asserted unchanged as well.
    hash_before = _hash_directory(staging_ws.root)
    global_reports_dir = DISCOVERY_DIR / "reports"
    global_exports_dir = DISCOVERY_DIR / "doi_candidates"

    def _tree_stats(root: Path) -> tuple[int, int]:
        if not root.is_dir():
            return (0, 0)
        files = [p for p in root.rglob("*") if p.is_file()]
        return (len(files), sum(p.stat().st_size for p in files))

    def _ledger_sha256() -> str:
        if not PAPER_NUMBER_LEDGER_PATH.is_file():
            return ""
        return hashlib.sha256(
            PAPER_NUMBER_LEDGER_PATH.read_bytes()
        ).hexdigest()

    def _paper_raw_ids() -> list[str]:
        if not PAPER_RAW_DIR.is_dir():
            return []
        return sorted(d.name for d in PAPER_RAW_DIR.iterdir() if d.is_dir())

    side_effects_before = {
        "reports": _tree_stats(global_reports_dir),
        "doi_candidates": _tree_stats(global_exports_dir),
        "paper_raw_ids": _paper_raw_ids(),
        "ledger_sha256": _ledger_sha256(),
    }

    smoke_report_paths: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="mineru_v4_smoke_") as tmpdir:
        clone_root = Path(tmpdir) / staging_ws.generation_id
        smoke_ws = _clone_workspace_for_smoke(staging_ws, clone_root)

        # Isolated smoke targets: everything the smoke run writes lives
        # inside the ephemeral clone and disappears with it.  Without these
        # overrides --stage-to-paper-raw --apply would write the production
        # paper_raw, papers, and paper-number ledger, and the default
        # --report-dir/--output-dir would write the global discovery
        # reports/exports trees.
        smoke_root = smoke_ws.root / "smoke"
        smoke_paper_raw_dir = smoke_root / "paper_raw"
        smoke_papers_dir = smoke_root / "papers"
        smoke_ledger_path = smoke_root / "paper_number_ledger.json"
        smoke_report_dir = smoke_root / "reports"
        smoke_output_dir = smoke_root / "exports"
        smoke_paper_raw_dir.mkdir(parents=True, exist_ok=True)
        smoke_papers_dir.mkdir(parents=True, exist_ok=True)
        smoke_report_dir.mkdir(parents=True, exist_ok=True)
        smoke_output_dir.mkdir(parents=True, exist_ok=True)
        print(f"  Smoke ephemeral clone: {smoke_ws.root}")
        print(f"  Smoke isolation: paper_raw={smoke_paper_raw_dir}")
        print(f"                   papers={smoke_papers_dir}")
        print(f"                   ledger={smoke_ledger_path}")
        print(f"                   reports={smoke_report_dir}")
        print(f"                   exports={smoke_output_dir}")

        # Import here to avoid circular import
        from scripts.discover_papers_concurrent import main_internal
        smoke_args = [
            "--from-enabled-notebooks",
            "--workspace-root", str(smoke_ws.root),
            "--mode", "hybrid",
            "--refresh-pages", "1",
            "--backfill-pages", "1",
            "--max-workers", "3",
            "--max-pages-total", "20",
            "--max-provider-requests-total", "40",
            "--max-candidates", "10",
            "--paper-raw-dir", str(smoke_paper_raw_dir),
            "--papers-dir", str(smoke_papers_dir),
            "--ledger-path", str(smoke_ledger_path),
            "--report-dir", str(smoke_report_dir),
            "--output-dir", str(smoke_output_dir),
            "--stage-to-paper-raw", "--apply",
        ]
        print(f"  Running: discover_papers_concurrent {' '.join(smoke_args)}")
        exit_code = main_internal(smoke_args)

        hash_after = _hash_directory(staging_ws.root)
        side_effects_after = {
            "reports": _tree_stats(global_reports_dir),
            "doi_candidates": _tree_stats(global_exports_dir),
            "paper_raw_ids": _paper_raw_ids(),
            "ledger_sha256": _ledger_sha256(),
        }
        smoke_report_paths = sorted(smoke_report_dir.glob("*.json"))
        smoke_report_hashes = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in smoke_report_paths
        }
        smoke_report_telemetry: dict[str, object] = {}
        if smoke_report_paths:
            try:
                latest = json.loads(
                    smoke_report_paths[-1].read_text(encoding="utf-8")
                )
                smoke_report_telemetry = (
                    latest.get("lane_aggregation", {}).get("provider_requests")
                    or {}
                )
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                smoke_report_telemetry = {}

    # The TemporaryDirectory has removed the ephemeral clone; only the
    # recorded evidence remains.
    journal.metadata["smoke_isolation"] = {
        "ephemeral_clone": True,
        "workspace_root": str(smoke_ws.root),
        "paper_raw_dir": str(smoke_paper_raw_dir),
        "papers_dir": str(smoke_papers_dir),
        "ledger_path": str(smoke_ledger_path),
        "report_dir": str(smoke_report_dir),
        "output_dir": str(smoke_output_dir),
    }
    journal.metadata["smoke_tree_hash"] = {
        "before": hash_before,
        "after": hash_after,
        "equal": hash_after == hash_before,
    }
    journal.metadata["smoke_side_effects"] = {
        "global_reports_before": side_effects_before["reports"],
        "global_reports_after": side_effects_after["reports"],
        "global_exports_before": side_effects_before["doi_candidates"],
        "global_exports_after": side_effects_after["doi_candidates"],
        "paper_raw_unchanged": (
            side_effects_before["paper_raw_ids"]
            == side_effects_after["paper_raw_ids"]
        ),
        "ledger_unchanged": (
            side_effects_before["ledger_sha256"]
            == side_effects_after["ledger_sha256"]
        ),
    }
    journal.metadata["smoke_receipt"] = {
        "exit_code": exit_code,
        "report_hashes": smoke_report_hashes,
        "provider_requests": smoke_report_telemetry,
    }

    side_effect_drift = (
        side_effects_before["reports"] != side_effects_after["reports"]
        or side_effects_before["doi_candidates"]
        != side_effects_after["doi_candidates"]
        or side_effects_before["paper_raw_ids"]
        != side_effects_after["paper_raw_ids"]
        or side_effects_before["ledger_sha256"]
        != side_effects_after["ledger_sha256"]
    )
    if side_effect_drift:
        print("  [ERROR] Smoke run leaked global side effects: "
              f"{journal.metadata['smoke_side_effects']}")
        print("  Migration blocked: the smoke run must be side-effect free.")
        if journal.state == MigrationState.PREFLIGHT_VALIDATED:
            journal.transition_to(MigrationState.SMOKE_FAILED)
        journal.save()
        return 1

    if hash_after != hash_before:
        print("  [ERROR] Smoke run mutated the staging workspace: tree hash "
              f"before={hash_before[:16]}... after={hash_after[:16]}...")
        print("  Migration blocked: the smoke run must be side-effect free.")
        if journal.state == MigrationState.PREFLIGHT_VALIDATED:
            journal.transition_to(MigrationState.SMOKE_FAILED)
        journal.save()
        return 1

    if exit_code != 0:
        print(f"  [ERROR] Smoke test exit code: {exit_code}")
        print("  Migration blocked: fix the smoke test failure before cutover.")
        if journal.state == MigrationState.PREFLIGHT_VALIDATED:
            journal.transition_to(MigrationState.SMOKE_FAILED)
        journal.save()
        return 1

    print("  [OK] Smoke test passed (staging workspace untouched: tree hash equal)")
    journal.transition_to(MigrationState.SMOKE_PASSED)
    journal.save()
    return 0


def _run_apply_from_state(
    journal: MigrationJournal,
    args: argparse.Namespace,
    *,
    resume: bool = False,
) -> int:
    """Run all remaining apply steps from the journal's current state.

    Idempotent: already-completed steps are skipped because the journal state
    has advanced past them.
    """
    staging_ws: DiscoveryWorkspace | None = None
    nb_results: list[dict[str, object]] = []

    if journal.state == MigrationState.PLANNED:
        _step_inventory(journal, args)

    if journal.state == MigrationState.INVENTORY_COMPLETE:
        _step_archive(journal, args)

    if journal.state == MigrationState.ARCHIVE_PREPARED:
        staging_ws = _step_build_workspace(journal, args)

    if journal.state == MigrationState.WORKSPACE_BUILT:
        if staging_ws is None:
            staging_ws = _resolve_staging_ws_from_journal(journal)
        nb_results = _step_migrate_notebooks(journal, staging_ws, args)

    if journal.state == MigrationState.NOTEBOOKS_STAGED:
        if staging_ws is None:
            staging_ws = _resolve_staging_ws_from_journal(journal)
        eligible = journal.metadata.get("eligible_legacy_candidates")
        if eligible == 0:
            # Automatic skip only: the inventory proved there are no legacy
            # candidates to carry over.  There is no operator flag for this.
            # A zeroed stats block keeps the preflight candidate-integrity
            # gates meaningful (0 unresolved, 0 quarantined, conservation 0=0,
            # pending count 0 == imported 0).
            print("[APPLY] Step 5/8: inventory reports "
                  "eligible_legacy_candidates=0; nothing to extract")
            journal.metadata["candidate_stats"] = {
                "journals_scanned": 0,
                "candidates_observed": 0,
                "valid_doi_seeds": 0,
                "invalid_doi": 0,
                "already_existing": 0,
                "duplicate_seeds": 0,
                "imported": 0,
                "terminal": 0,
                "quarantined": 0,
                "unresolved": 0,
                "errors": [],
            }
            journal.transition_to(MigrationState.CANDIDATES_EXTRACTED)
            journal.save()
        else:
            _step_extract_candidates(journal, staging_ws, args)

    if journal.state == MigrationState.CANDIDATES_EXTRACTED:
        if staging_ws is None:
            staging_ws = _resolve_staging_ws_from_journal(journal)
        if not nb_results:
            # Reconstruct nb_results from the workspace for resume idempotency.
            nb_results = []
            for nb_path in sorted(staging_ws.keyword_notebook_dir.glob("*.json")):
                data = json.loads(nb_path.read_text(encoding="utf-8"))
                active = sum(
                    1 for q in data.get("search_queries", {}).values()
                    if isinstance(q, dict) and q.get("active")
                )
                nb_results.append({
                    "success": True,
                    "keyword_zh": data.get("keyword_zh", nb_path.stem),
                    "active_queries": active,
                    "lane_count": active * 2 * 2,
                })
        _step_preflight(journal, staging_ws, nb_results, args)

    if journal.state in (MigrationState.PREFLIGHT_VALIDATED, MigrationState.SMOKE_FAILED):
        if staging_ws is None:
            staging_ws = _resolve_staging_ws_from_journal(journal)
        smoke_rc = _step_smoke(journal, staging_ws, args)
        if smoke_rc != 0:
            return smoke_rc

    if journal.state == MigrationState.SMOKE_PASSED:
        print(f"\n[APPLY] Migration ready for cutover.")
        if staging_ws is None:
            staging_ws = _resolve_staging_ws_from_journal(journal)
        print(f"  Staging workspace: {staging_ws.root}")
        print(f"  Notebooks: {staging_ws.keyword_notebook_dir}")
        print(f"  To activate, run:")
        print(f"    python scripts/migrate_discovery_v4.py --cutover {journal.migration_id}")
        return 0

    return 0


# ── Top-level commands ────────────────────────────────────────────────────


def _maintenance_lock_path() -> Path:
    """Global maintenance lock lives next to the migration journals.

    Read from the journal module at call time so ``--migrations-dir``
    overrides and test path patching are honored.
    """
    return _journal_mod.DISCOVERY_MIGRATIONS_DIR / ".migration.lock"


def cmd_apply(args: argparse.Namespace) -> int:
    """Execute the full migration."""
    migration_id = args.migration_id or f"v4-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    print(f"[APPLY] Migration ID: {migration_id}")

    try:
        with MigrationMaintenanceLock(
            "apply", migration_id=migration_id, lock_path=_maintenance_lock_path()
        ):
            return _cmd_apply_locked(args, migration_id)
    except MigrationMaintenanceLockError as exc:
        print(f"[APPLY] Error: {exc}", file=sys.stderr)
        return 1


def _cmd_apply_locked(args: argparse.Namespace, migration_id: str) -> int:
    # Re-create a fresh journal if the old one is not in a terminal state.
    try:
        journal = MigrationJournal.load(migration_id)
        if journal.state in {MigrationState.CUTOVER_COMMITTED, MigrationState.LEGACY_CLEANED,
                              MigrationState.FINALIZED, MigrationState.ABORTED}:
            print(f"[APPLY] Migration {migration_id} is already in terminal state {journal.state.value}",
                  file=sys.stderr)
            return 1
        print(f"[APPLY] Resuming existing journal from state {journal.state.value}")
    except FileNotFoundError:
        journal = MigrationJournal.create(migration_id=migration_id)

    try:
        return _run_apply_from_state(journal, args, resume=False)
    except MigrationStepError as exc:
        print(f"[APPLY] Error: {exc}", file=sys.stderr)
        return 1


def cmd_resume(args: argparse.Namespace) -> int:
    """Resume an interrupted migration."""
    migration_id = args.resume
    if not migration_id:
        print("[RESUME] Error: --resume requires a migration ID", file=sys.stderr)
        return 1

    try:
        with MigrationMaintenanceLock(
            "resume", migration_id=migration_id, lock_path=_maintenance_lock_path()
        ):
            return _cmd_resume_locked(args, migration_id)
    except MigrationMaintenanceLockError as exc:
        print(f"[RESUME] Error: {exc}", file=sys.stderr)
        return 1


def _cmd_resume_locked(args: argparse.Namespace, migration_id: str) -> int:
    journal = MigrationJournal.load(migration_id)
    print(f"[RESUME] Migration: {migration_id}, state: {journal.state.value}")
    for t in journal.transitions:
        print(f"  {t['from']} → {t['to']} at {t['at']}")

    if journal.state in {MigrationState.FINALIZED, MigrationState.ABORTED}:
        print(f"[RESUME] Migration is in terminal state {journal.state.value}; nothing to resume.")
        return 0

    if journal.state == MigrationState.CUTOVER_COMMITTED:
        print(f"[RESUME] Cutover committed; continue the post-cutover chain:")
        print(f"    python scripts/migrate_discovery_v4.py --post-cutover-validate {migration_id}")
        print(f"    python scripts/migrate_discovery_v4.py --clean-legacy {migration_id}")
        print(f"    python scripts/migrate_discovery_v4.py --finalize {migration_id}")
        print(f"  (or revert with: --rollback {migration_id})")
        return 0

    if journal.state == MigrationState.LEGACY_CLEANED:
        print(f"[RESUME] Legacy archive cleaned; finish with:")
        print(f"    python scripts/migrate_discovery_v4.py --finalize {migration_id}")
        return 0

    if journal.state == MigrationState.SMOKE_PASSED:
        print(f"[RESUME] Migration is ready for cutover.")
        print(f"  To activate, run:")
        print(f"    python scripts/migrate_discovery_v4.py --cutover {migration_id}")
        return 0

    if journal.state == MigrationState.SMOKE_FAILED:
        print("[RESUME] Retrying smoke test...")

    try:
        return _run_apply_from_state(journal, args, resume=True)
    except MigrationStepError as exc:
        print(f"[RESUME] Error: {exc}", file=sys.stderr)
        return 1


def _expected_generation_id(journal: MigrationJournal) -> str:
    """Generation id this migration promotes (staging root name)."""
    root_str = journal.metadata.get("staging_workspace")
    if root_str:
        return Path(root_str).name
    return journal.migration_id


def _validate_active_workspace(
    journal: MigrationJournal,
    *,
    verify_tree: bool = False,
) -> DiscoveryWorkspace:
    """Read-only validation that the active generation is complete and
    hash-bound to this migration.  Raises RuntimeError on any failure.

    ``verify_tree=True`` additionally recomputes the workspace tree hash
    against the manifest.  It is only meaningful in the migration window
    right after cutover, before any production discovery run has mutated the
    workspace; identity-only validation is used everywhere else so the
    post-cutover chain stays valid after production runs begin.
    """
    resolver = WorkspaceResolver()
    ws = resolver.resolve_active(verify_tree=verify_tree)
    expected = _expected_generation_id(journal)
    if ws.generation_id != expected:
        raise MigrationStepError(
            f"active generation {ws.generation_id!r} does not match this "
            f"migration's generation {expected!r}"
        )
    pointer = resolver.resolve_pointer()
    if pointer.migration_id != journal.migration_id:
        raise MigrationStepError(
            f"active pointer migration_id {pointer.migration_id!r} does not "
            f"match journal migration {journal.migration_id!r}"
        )
    return ws


def cmd_cutover(args: argparse.Namespace) -> int:
    """Promote the staging workspace to the active v4 generation.

    Idempotent: a rerun after a crash between rename / pointer write / journal
    save reconciles the filesystem state instead of failing.
    """
    migration_id = args.cutover
    if not migration_id:
        print("[CUTOVER] Error: --cutover requires a migration ID", file=sys.stderr)
        return 1

    try:
        with MigrationMaintenanceLock(
            "cutover", migration_id=migration_id, lock_path=_maintenance_lock_path()
        ):
            return _cmd_cutover_locked(args, migration_id)
    except MigrationMaintenanceLockError as exc:
        print(f"[CUTOVER] Error: {exc}", file=sys.stderr)
        return 1


def _cmd_cutover_locked(args: argparse.Namespace, migration_id: str) -> int:
    journal = MigrationJournal.load(migration_id)
    print(f"[CUTOVER] Migration: {migration_id}, state: {journal.state.value}")

    if args.skip_real_smoke:
        print("[CUTOVER] Error: --skip-real-smoke never grants cutover eligibility; "
              "run the real smoke test to reach smoke_passed first.", file=sys.stderr)
        return 1

    if journal.state == MigrationState.CUTOVER_COMMITTED:
        # Journal already advanced; verify the filesystem agrees and exit.
        try:
            _validate_active_workspace(journal)
        except RuntimeError as exc:
            print(f"[CUTOVER] Error: journal is cutover_committed but the active "
                  f"workspace does not validate: {exc}", file=sys.stderr)
            return 1
        print(f"[CUTOVER] Migration {migration_id} already committed; nothing to do.")
        return 0

    allowed_cutover_states = {MigrationState.SMOKE_PASSED}
    if journal.state not in allowed_cutover_states:
        print(f"[CUTOVER] Error: cannot cutover from state {journal.state.value}. "
              f"Required: {sorted(s.value for s in allowed_cutover_states)}", file=sys.stderr)
        return 1

    # Resolve the workspace to promote.  A previous attempt that crashed after
    # the rename leaves no staging directory; reconcile from the promoted
    # generation instead of rebuilding the manifest.
    staging_root_str = journal.metadata.get("staging_workspace")
    staging_root = Path(staging_root_str) if staging_root_str else None
    if staging_root is not None and staging_root.is_dir():
        commit_ws = _resolve_staging_ws_from_journal(journal)
        print(f"[CUTOVER] Staging workspace: {commit_ws.root}")
        total_lanes = journal.metadata.get("total_lanes", 0)
        inventory_sha256 = journal.metadata.get("aggregate_sha256")
        _, manifest_sha256 = _build_workspace_manifest(
            commit_ws,
            migration_id,
            inventory_sha256,
            total_lanes,
        )
    else:
        generation_id = _expected_generation_id(journal)
        target_root = DISCOVERY_GENERATIONS_DIR / generation_id
        manifest_path = target_root / "workspace.json"
        if not manifest_path.is_file():
            print(f"[CUTOVER] Error: neither staging workspace nor promoted "
                  f"generation exists for {generation_id!r}; cannot reconcile.",
                  file=sys.stderr)
            return 1
        print(f"[CUTOVER] Staging already promoted; reconciling from: {target_root}")
        manifest_sha256 = _sha256_file(manifest_path)
        commit_ws = _workspace_at_root(target_root)

    activated_at = datetime.now(timezone.utc).isoformat()
    pointer = ActiveGenerationPointerV4(
        generation_id=commit_ws.generation_id,
        workspace_manifest_sha256=manifest_sha256,
        activated_at=activated_at,
        migration_id=migration_id,
    )

    lock_path = journal.path.parent / ".migration.lock"
    snapshot_path = journal.path.parent / f"{migration_id}.previous_pointer.json"
    try:
        commit_workspace(
            commit_ws,
            pointer,
            lock_path=lock_path,
            previous_pointer_snapshot_path=snapshot_path,
        )
    except CutoverLockError as exc:
        print(f"[CUTOVER] Error: {exc}", file=sys.stderr)
        return 1
    except CutoverReconciliationError as exc:
        print(f"[CUTOVER] Error: cannot reconcile cutover state: {exc}", file=sys.stderr)
        return 1
    print(f"[CUTOVER] Active generation committed: {commit_ws.generation_id}")
    print(f"[CUTOVER] Activated at: {activated_at}")

    journal.transition_to(MigrationState.CUTOVER_COMMITTED)
    journal.save()
    print(f"[CUTOVER] Migration journal state: {journal.state.value}")
    return 0


def cmd_post_cutover_validate(args: argparse.Namespace) -> int:
    """Read-only validation of the active workspace after cutover."""
    migration_id = args.post_cutover_validate
    if not migration_id:
        print("[VALIDATE] Error: --post-cutover-validate requires a migration ID",
              file=sys.stderr)
        return 1

    journal = MigrationJournal.load(migration_id)
    print(f"[VALIDATE] Migration: {migration_id}, state: {journal.state.value}")

    if journal.state != MigrationState.CUTOVER_COMMITTED:
        print(f"[VALIDATE] Error: post-cutover validation requires state "
              f"cutover_committed, got {journal.state.value}", file=sys.stderr)
        return 1

    try:
        ws = _validate_active_workspace(journal)
    except RuntimeError as exc:
        print(f"[VALIDATE] Error: active workspace validation failed: {exc}",
              file=sys.stderr)
        return 1

    # Pending store lifecycle gate: the pending store is a transitional
    # channel for migrated legacy candidates.  It must be fully drained by a
    # normal discovery run before the post-cutover chain may proceed;
    # --clean-legacy removes its directory.
    pending_store = PendingCandidateStoreV4(ws)
    pending_remaining = pending_store.count()
    if pending_remaining:
        print(f"[VALIDATE] Error: pending candidate store is not drained: "
              f"{pending_remaining} candidate file(s) remain under "
              f"{ws.pending_candidates_dir}", file=sys.stderr)
        print("[VALIDATE] Run a normal discovery batch to drain the migrated "
              "candidates into paper_raw, e.g.:", file=sys.stderr)
        print("    python scripts/discover_papers_concurrent.py "
              "--from-enabled-notebooks --stage-to-paper-raw --apply ...",
              file=sys.stderr)
        print(f"[VALIDATE] Then re-run: python scripts/migrate_discovery_v4.py "
              f"--post-cutover-validate {migration_id}", file=sys.stderr)
        return 1

    # The activation-time tree closure is enforced only when no legacy
    # candidates were staged into the pending store.  A migration that
    # imported candidates requires a production drain run before this
    # validation, and that run legitimately mutates the runtime tree
    # (lane states, page journals, drained candidate files), so the
    # activation-time tree hash can no longer match the manifest.  Runtime
    # content integrity is per-record (checksums, atomic writes), never the
    # whole-tree hash; see docs/ADR_DISCOVERY_V4_MIGRATION_FINAL.md.
    candidate_stats = journal.metadata.get("candidate_stats")
    imported = 0
    if isinstance(candidate_stats, dict):
        try:
            imported = int(candidate_stats.get("imported", 0) or 0)
        except (TypeError, ValueError):
            imported = 0
    if imported == 0:
        try:
            # The designated content check of the post-cutover chain: with
            # nothing to drain, no production run is required, so the
            # activation-time tree closure must still match the manifest
            # exactly.
            _validate_active_workspace(journal, verify_tree=True)
        except RuntimeError as exc:
            print(f"[VALIDATE] Error: active workspace validation failed: {exc}",
                  file=sys.stderr)
            return 1
    else:
        print(f"[VALIDATE] Pending store drained ({imported} migrated "
              "candidate(s) consumed by production discovery); activation-time "
              "tree closure legitimately superseded by the drain run — "
              "identity and manifest checks passed.")

    # Receipt gate: an empty pending store is never sufficient proof.  When
    # candidates were imported, the closed post-cutover reconciliation
    # (per-seed durable receipts verified against paper_raw and the ledger)
    # must exist and must account for exactly the imported set.
    if imported > 0:
        report_path = (
            DISCOVERY_MIGRATIONS_DIR
            / f"{migration_id}.post_cutover_reconciliation.json"
        )
        receipts_dir = DISCOVERY_MIGRATIONS_DIR / f"{migration_id}.receipts"
        receipt_errors: list[str] = []
        report: dict[str, object] = {}
        if not report_path.is_file():
            receipt_errors.append(
                f"post-cutover reconciliation report missing: {report_path}; "
                f"run: python scripts/reconcile_discovery_v4_migration.py "
                f"--migration-id {migration_id} --apply"
            )
        else:
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                receipt_errors.append(
                    f"reconciliation report unreadable: {exc}"
                )
        if report:
            if report.get("migration_id") != migration_id:
                receipt_errors.append("reconciliation report migration_id mismatch")
            if int(report.get("unresolved_items", -1)) != 0:
                receipt_errors.append(
                    f"reconciliation not closed: unresolved_items="
                    f"{report.get('unresolved_items')}"
                )
            if int(report.get("receipts_verified", -1)) != imported:
                receipt_errors.append(
                    f"reconciliation verified {report.get('receipts_verified')} "
                    f"seed(s) but candidate_stats.imported={imported}"
                )
        receipt_count = (
            sum(1 for _ in receipts_dir.glob("*.json"))
            if receipts_dir.is_dir() else 0
        )
        if receipt_count != imported:
            receipt_errors.append(
                f"seed receipts on disk: {receipt_count}, expected {imported}"
            )
        if receipt_errors:
            print(f"[VALIDATE] Error: migration receipt gate failed "
                  f"({len(receipt_errors)}):", file=sys.stderr)
            for err in receipt_errors:
                print(f"  - {err}", file=sys.stderr)
            return 1
        print(f"[VALIDATE] Receipt gate OK: {receipt_count} per-seed receipts "
              "verified by the closed reconciliation.")

    print(f"[VALIDATE] Active generation OK: {ws.generation_id}")
    print(f"[VALIDATE] Root: {ws.root}")
    print("[VALIDATE] Next steps:")
    print(f"    python scripts/migrate_discovery_v4.py --clean-legacy {migration_id}")
    print(f"    python scripts/migrate_discovery_v4.py --finalize {migration_id}")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    """Roll back a committed cutover: restore the previous active pointer and
    move the promoted generation back to staging."""
    migration_id = args.rollback
    if not migration_id:
        print("[ROLLBACK] Error: --rollback requires a migration ID", file=sys.stderr)
        return 1

    try:
        with MigrationMaintenanceLock(
            "rollback", migration_id=migration_id, lock_path=_maintenance_lock_path()
        ):
            return _cmd_rollback_locked(migration_id)
    except MigrationMaintenanceLockError as exc:
        print(f"[ROLLBACK] Error: {exc}", file=sys.stderr)
        return 1


def _cmd_rollback_locked(migration_id: str) -> int:
    journal = MigrationJournal.load(migration_id)
    print(f"[ROLLBACK] Migration: {migration_id}, state: {journal.state.value}")

    if journal.state != MigrationState.CUTOVER_COMMITTED:
        print(f"[ROLLBACK] Error: rollback is only allowed from cutover_committed "
              f"(legacy not yet cleaned), got {journal.state.value}. "
              "Use --abort for pre-cutover states.", file=sys.stderr)
        return 1

    snapshot_path = journal.path.parent / f"{migration_id}.previous_pointer.json"
    if not snapshot_path.is_file():
        print(f"[ROLLBACK] Error: previous-pointer snapshot missing: {snapshot_path}; "
              "cannot restore the pre-cutover pointer.", file=sys.stderr)
        return 1
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        print(f"[ROLLBACK] Error: cannot read previous-pointer snapshot: {exc}",
              file=sys.stderr)
        return 1
    previous_pointer = snapshot.get("previous_pointer")
    if previous_pointer is not None:
        try:
            ActiveGenerationPointerV4.from_dict_strict(previous_pointer)
        except (ValueError, TypeError) as exc:
            print(f"[ROLLBACK] Error: snapshot previous pointer is not strict V4: {exc}",
                  file=sys.stderr)
            return 1

    generation_id = _expected_generation_id(journal)
    target_root = DISCOVERY_GENERATIONS_DIR / generation_id
    staging_target = STAGING_DIR / generation_id

    # The maintenance lock (acquired by cmd_rollback) is the sole mutex for
    # this mutation window.
    if staging_target.exists():
        print(f"[ROLLBACK] Error: staging path already exists: {staging_target}; "
              "refusing to overwrite.", file=sys.stderr)
        return 1
    if target_root.is_dir():
        print(f"[ROLLBACK] Moving generation back to staging: {target_root}")
        staging_target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(str(target_root), str(staging_target))
    if previous_pointer is not None:
        _atomic_write_json(ACTIVE_GENERATION_PATH, previous_pointer)
        print(f"[ROLLBACK] Restored previous active pointer: "
              f"{previous_pointer.get('generation_id')}")
    else:
        try:
            ACTIVE_GENERATION_PATH.unlink()
            print("[ROLLBACK] Removed active pointer (no previous generation).")
        except FileNotFoundError:
            pass

    journal.metadata["rollback"] = {
        "rolled_back_at": datetime.now(timezone.utc).isoformat(),
        "generation_id": generation_id,
        "restored_previous_pointer": previous_pointer is not None,
        "snapshot_path": str(snapshot_path),
    }
    journal.transition_to(MigrationState.ABORTED)
    journal.save()
    print(f"[ROLLBACK] Migration {migration_id} rolled back. State: {journal.state.value}")
    return 0


def _verify_legacy_archive(archive_root: Path) -> list[str]:
    """Re-verify every archived file against its manifest.  Returns errors."""
    errors: list[str] = []
    for section in ("pending_pages", "keyword_notebooks"):
        manifest_path = archive_root / section / "archive_manifest.json"
        if not manifest_path.is_file():
            errors.append(f"archive manifest missing: {manifest_path}")
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            errors.append(f"cannot read archive manifest {manifest_path}: {exc}")
            continue
        for entry in manifest.get("files", []):
            rel = entry.get("path")
            expected_sha = entry.get("sha256")
            if not rel or not expected_sha:
                errors.append(f"archive manifest {section}: malformed entry {entry!r}")
                continue
            # Manifest path conventions differ per section: pending_pages
            # entries are relative to the discovery dir (they include the
            # "pending_pages/" prefix), notebook entries are bare file names
            # inside the keyword_notebooks archive dir.
            if section == "keyword_notebooks":
                file_path = archive_root / section / rel
            else:
                file_path = archive_root / rel
            if not file_path.is_file():
                errors.append(f"archived file missing: {file_path}")
                continue
            actual = _sha256_file(file_path)
            if actual != expected_sha:
                errors.append(
                    f"archived file hash mismatch: {rel} "
                    f"(manifest {expected_sha[:16]}..., actual {actual[:16]}...)"
                )
    return errors


def cmd_clean_legacy(args: argparse.Namespace) -> int:
    """Delete the legacy archive after the active workspace validates."""
    migration_id = args.clean_legacy
    if not migration_id:
        print("[CLEAN] Error: --clean-legacy requires a migration ID", file=sys.stderr)
        return 1

    try:
        with MigrationMaintenanceLock(
            "clean-legacy", migration_id=migration_id,
            lock_path=_maintenance_lock_path(),
        ):
            return _cmd_clean_legacy_locked(args, migration_id)
    except MigrationMaintenanceLockError as exc:
        print(f"[CLEAN] Error: {exc}", file=sys.stderr)
        return 1


def _cmd_clean_legacy_locked(args: argparse.Namespace, migration_id: str) -> int:
    journal = MigrationJournal.load(migration_id)
    print(f"[CLEAN] Migration: {migration_id}, state: {journal.state.value}")

    if journal.state != MigrationState.CUTOVER_COMMITTED:
        print(f"[CLEAN] Error: legacy cleanup requires state cutover_committed, "
              f"got {journal.state.value}", file=sys.stderr)
        return 1

    try:
        ws = _validate_active_workspace(journal)
    except RuntimeError as exc:
        print(f"[CLEAN] Error: active workspace validation failed: {exc}",
              file=sys.stderr)
        return 1
    print(f"[CLEAN] Active generation validated: {ws.generation_id}")

    # Pending store lifecycle gate: the pending store is a transitional
    # channel for migrated legacy candidates.  It must be fully drained
    # before its directory can be removed; a non-empty store means un-staged
    # migration carry-over and is never deleted.
    pending_store = PendingCandidateStoreV4(ws)
    pending_remaining = pending_store.count()
    if pending_remaining:
        print(f"[CLEAN] Error: pending candidate store is not drained: "
              f"{pending_remaining} candidate file(s) remain under "
              f"{ws.pending_candidates_dir}; refusing to remove it.  Run a "
              "normal discovery batch to drain the migrated candidates into "
              "paper_raw first.", file=sys.stderr)
        return 1
    if ws.pending_candidates_dir.is_dir():
        print(f"[CLEAN] Removing drained transitional pending store: "
              f"{ws.pending_candidates_dir}")
        shutil.rmtree(ws.pending_candidates_dir)

    archive_root = DISCOVERY_LEGACY_ARCHIVE_DIR / migration_id
    if not archive_root.is_dir():
        print(f"[CLEAN] Error: legacy archive missing: {archive_root}", file=sys.stderr)
        return 1
    errors = _verify_legacy_archive(archive_root)
    if errors:
        print(f"[CLEAN] Error: legacy archive failed verification ({len(errors)}):",
              file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(f"[CLEAN] Removing legacy archive: {archive_root}")
    shutil.rmtree(archive_root)
    journal.metadata["legacy_cleanup"] = {
        "cleaned_at": datetime.now(timezone.utc).isoformat(),
        "archive_root": str(archive_root),
        "archive_verified": True,
        "pending_store_drained": True,
        "pending_candidates_dir_removed": str(ws.pending_candidates_dir),
    }
    journal.transition_to(MigrationState.LEGACY_CLEANED)
    journal.save()
    print(f"[CLEAN] Migration journal state: {journal.state.value}")
    print(f"[CLEAN] Next step:")
    print(f"    python scripts/migrate_discovery_v4.py --finalize {migration_id}")
    return 0


def cmd_finalize(args: argparse.Namespace) -> int:
    """Write finalize evidence and close the migration."""
    migration_id = args.finalize
    if not migration_id:
        print("[FINALIZE] Error: --finalize requires a migration ID", file=sys.stderr)
        return 1

    try:
        with MigrationMaintenanceLock(
            "finalize", migration_id=migration_id,
            lock_path=_maintenance_lock_path(),
        ):
            return _cmd_finalize_locked(args, migration_id)
    except MigrationMaintenanceLockError as exc:
        print(f"[FINALIZE] Error: {exc}", file=sys.stderr)
        return 1


def _cmd_finalize_locked(args: argparse.Namespace, migration_id: str) -> int:
    journal = MigrationJournal.load(migration_id)
    print(f"[FINALIZE] Migration: {migration_id}, state: {journal.state.value}")

    if journal.state != MigrationState.LEGACY_CLEANED:
        print(f"[FINALIZE] Error: finalize requires state legacy_cleaned, "
              f"got {journal.state.value}", file=sys.stderr)
        return 1

    try:
        ws = _validate_active_workspace(journal)
    except RuntimeError as exc:
        print(f"[FINALIZE] Error: active workspace validation failed: {exc}",
              file=sys.stderr)
        return 1
    pointer = WorkspaceResolver().resolve_pointer()

    journal.metadata["finalize"] = {
        "finalized_at": datetime.now(timezone.utc).isoformat(),
        "active_generation_id": ws.generation_id,
        "workspace_manifest_sha256": pointer.workspace_manifest_sha256,
        "previous_generation_id": pointer.previous_generation_id,
        "validation": "resolve_active_ok",
    }
    journal.transition_to(MigrationState.FINALIZED)
    journal.save()
    print(f"[FINALIZE] Migration {migration_id} finalized. "
          f"Active generation: {ws.generation_id}")
    return 0


def cmd_retire_legacy_sources(args: argparse.Namespace) -> int:
    """Retire the flat legacy dirs into the retained tree (finalized only)."""
    migration_id = args.retire_legacy_sources
    if not migration_id:
        print("[RETIRE] Error: --retire-legacy-sources requires a migration ID",
              file=sys.stderr)
        return 1

    try:
        with MigrationMaintenanceLock(
            "retire-legacy-sources", migration_id=migration_id,
            lock_path=_maintenance_lock_path(),
        ):
            return _cmd_retire_legacy_sources_locked(args, migration_id)
    except MigrationMaintenanceLockError as exc:
        print(f"[RETIRE] Error: {exc}", file=sys.stderr)
        return 1


def _cmd_retire_legacy_sources_locked(args: argparse.Namespace, migration_id: str) -> int:
    journal = MigrationJournal.load(migration_id)
    print(f"[RETIRE] Migration: {migration_id}, state: {journal.state.value}")

    if journal.state != MigrationState.FINALIZED:
        print(f"[RETIRE] Error: legacy retirement requires state finalized, "
              f"got {journal.state.value}", file=sys.stderr)
        return 1

    retained_root = DISCOVERY_DIR / "legacy_retained"
    report_path = DISCOVERY_MIGRATIONS_DIR / f"{migration_id}.post_cutover_reconciliation.json"
    try:
        result = retire_legacy_sources(
            migration_id=migration_id,
            flat_notebooks_dir=DISCOVERY_KEYWORD_NOTEBOOK_DIR,
            flat_pending_pages_dir=DISCOVERY_PENDING_PAGES_DIR,
            retained_root=retained_root,
            reconciliation_report_path=report_path,
            retention_days=args.retention_days,
        )
    except LegacyRetirementError as exc:
        print(f"[RETIRE] Error: {exc}", file=sys.stderr)
        return 1

    # History is append-only: the earlier `legacy_cleanup` entry recorded the
    # archive-copy deletion and stays untouched; this entry records the real
    # retirement of the flat legacy sources.
    journal.metadata["legacy_retirement"] = result
    journal.save()

    print(f"[RETIRE] Legacy sources retired under {retained_root / migration_id}")
    print(f"[RETIRE] keyword_notebooks: {result['manifests']['keyword_notebooks']['file_count']} file(s)")
    print(f"[RETIRE] pending_pages: {result['manifests']['pending_pages']['file_count']} file(s)")
    print(f"[RETIRE] Tombstones written at the original flat paths; the retained "
          f"tree is read-only until {result['purge_not_before']}.")
    print(f"[RETIRE] Next steps:")
    print(f"    - Re-run post-cutover reconciliation; it auto-discovers the retained pages.")
    print(f"    - After the retention window, purge with:")
    print(f"      python scripts/migrate_discovery_v4.py --purge-retained-legacy "
          f"{migration_id} --confirm-migration-id {migration_id}")
    return 0


def cmd_purge_retained_legacy(args: argparse.Namespace) -> int:
    """Purge the retained legacy tree after its retention window ends."""
    migration_id = args.purge_retained_legacy
    if not migration_id:
        print("[PURGE] Error: --purge-retained-legacy requires a migration ID",
              file=sys.stderr)
        return 1
    if not args.confirm_migration_id:
        print("[PURGE] Error: --purge-retained-legacy requires "
              "--confirm-migration-id equal to the migration ID", file=sys.stderr)
        return 1

    try:
        with MigrationMaintenanceLock(
            "purge-retained-legacy", migration_id=migration_id,
            lock_path=_maintenance_lock_path(),
        ):
            return _cmd_purge_retained_legacy_locked(args, migration_id)
    except MigrationMaintenanceLockError as exc:
        print(f"[PURGE] Error: {exc}", file=sys.stderr)
        return 1


def _cmd_purge_retained_legacy_locked(args: argparse.Namespace, migration_id: str) -> int:
    journal = MigrationJournal.load(migration_id)
    print(f"[PURGE] Migration: {migration_id}, state: {journal.state.value}")

    if journal.state != MigrationState.FINALIZED:
        print(f"[PURGE] Error: legacy purge requires state finalized, "
              f"got {journal.state.value}", file=sys.stderr)
        return 1

    try:
        result = purge_retained_legacy(
            migration_id=migration_id,
            retained_root=DISCOVERY_DIR / "legacy_retained",
            confirm_migration_id=args.confirm_migration_id,
            now=datetime.now(timezone.utc),
            active_generation_path=ACTIVE_GENERATION_PATH,
        )
    except LegacyRetirementError as exc:
        print(f"[PURGE] Error: {exc}", file=sys.stderr)
        return 1

    journal.metadata["legacy_purged"] = result
    journal.save()
    print(f"[PURGE] Retained legacy tree purged: {result['purged_tree']}")
    print(f"[PURGE] Tombstones removed: {len(result['tombstones_removed'])}")
    return 0


def cmd_abort(args: argparse.Namespace) -> int:
    """Abort a migration and optionally clean up the staging workspace."""
    migration_id = args.abort
    if not migration_id:
        print("[ABORT] Error: --abort requires a migration ID", file=sys.stderr)
        return 1

    try:
        with MigrationMaintenanceLock(
            "abort", migration_id=migration_id,
            lock_path=_maintenance_lock_path(),
        ):
            return _cmd_abort_locked(args, migration_id)
    except MigrationMaintenanceLockError as exc:
        print(f"[ABORT] Error: {exc}", file=sys.stderr)
        return 1


def _cmd_abort_locked(args: argparse.Namespace, migration_id: str) -> int:
    journal = MigrationJournal.load(migration_id)
    print(f"[ABORT] Migration: {migration_id}, state: {journal.state.value}")

    if journal.state in {MigrationState.CUTOVER_COMMITTED, MigrationState.LEGACY_CLEANED,
                         MigrationState.FINALIZED, MigrationState.ABORTED}:
        print(f"[ABORT] Error: cannot abort migration in terminal state {journal.state.value}",
              file=sys.stderr)
        return 1

    staging_root_str = journal.metadata.get("staging_workspace")
    if staging_root_str:
        staging_root = Path(staging_root_str)
        # Only delete staging workspaces that live inside the designated staging directory.
        try:
            staging_root.relative_to(STAGING_DIR)
            inside_staging = True
        except ValueError:
            inside_staging = False
        if staging_root.exists() and inside_staging:
            print(f"[ABORT] Removing staging workspace: {staging_root}")
            shutil.rmtree(staging_root)

    journal.transition_to(MigrationState.ABORTED)
    journal.save()
    print(f"[ABORT] Migration {migration_id} aborted. State: {journal.state.value}")
    return 0


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


def _dry_run_candidate_probe() -> int:
    """Read-only strict candidate extraction + conservation over live legacy pages.

    Runs the same strict journal reader the apply pipeline uses and asserts
    candidate conservation on the observed counters.  Nothing is written:
    no quarantine file, no pending candidates, no journal.
    """
    print("[DRY-RUN] Validating legacy candidate extraction (read-only)...")
    stats: dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix="mineru_v4_dryrun_index_") as index_dir:
        build_known_doi_index(
            ledger_path=PAPER_NUMBER_LEDGER_PATH,
            papers_dir=PAPERS_DIR,
            paper_raw_dir=PAPER_RAW_DIR,
            db_path=Path(index_dir) / "known_dois.sqlite",
        )
        with SqliteDoiIndex(Path(index_dir) / "known_dois.sqlite") as known_index, \
                SqliteDoiIndex(Path(index_dir) / "batch_dois.sqlite") as batch_index:
            try:
                for _seed in stream_extract_candidates(
                    DISCOVERY_PENDING_PAGES_DIR,
                    known_doi_index=known_index,
                    batch_index=batch_index,
                    stats=stats,
                ):
                    pass
            except LegacyPageJournalContractError as exc:
                print(f"[DRY-RUN] Error: legacy page journal failed strict "
                      f"validation: {exc}", file=sys.stderr)
                return 1
    report = CandidateExtractionReport(
        journals_scanned=stats.get("journals_scanned", 0),
        candidates_observed=stats.get("candidates_observed", 0),
        valid_doi_seeds=stats.get("valid_doi_seeds", 0),
        invalid_doi=stats.get("invalid_doi", 0),
        already_existing=stats.get("already_existing", 0),
        duplicate_seeds=stats.get("duplicate_seeds", 0),
        imported=stats.get("valid_doi_seeds", 0),
        terminal=stats.get("terminal", 0),
        unresolved=0,
        errors=[],
    )
    try:
        assert_conservation(report)
    except CandidateConservationError as exc:
        print(f"[DRY-RUN] Error: {exc}", file=sys.stderr)
        return 1
    print(f"[DRY-RUN] Candidate extraction OK: {report.candidates_observed} "
          f"observed, {report.valid_doi_seeds} importable (conservation holds)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    _apply_dir_overrides(args)

    if args.inspect:
        return cmd_inspect(args)
    if args.resume:
        return cmd_resume(args)
    if args.cutover:
        return cmd_cutover(args)
    if args.post_cutover_validate:
        return cmd_post_cutover_validate(args)
    if args.rollback:
        return cmd_rollback(args)
    if args.clean_legacy:
        return cmd_clean_legacy(args)
    if args.finalize:
        return cmd_finalize(args)
    if args.retire_legacy_sources:
        return cmd_retire_legacy_sources(args)
    if args.purge_retained_legacy:
        return cmd_purge_retained_legacy(args)
    if args.abort:
        return cmd_abort(args)
    if args.dry_run:
        print("[DRY-RUN] Running plan + notebook migration validation without cutover...")
        plan_rc = cmd_plan(args)
        if plan_rc != 0:
            return plan_rc
        # Validate notebook migration in a temporary directory; no real staging workspace.
        with tempfile.TemporaryDirectory(prefix="mineru_v4_dryrun_") as tmpdir:
            tmp_path = Path(tmpdir)
            nb_results = migrate_all_notebooks(
                notebook_dir=DISCOVERY_KEYWORD_NOTEBOOK_DIR,
                output_dir=tmp_path,
            )
            success = sum(1 for r in nb_results if r["success"])
            print(f"[DRY-RUN] Notebook migration: {success}/{len(nb_results)} success")
            for r in nb_results:
                status = "OK" if r["success"] else "FAIL"
                print(f"  [{status}] {r['keyword_zh']}: {r.get('active_queries', 0)} queries, {r.get('lane_count', 0)} lanes")
                if not r["success"]:
                    print(f"         Error: {r.get('error', 'unknown')}")
        if success != len(nb_results):
            print(f"[DRY-RUN] Error: notebook migration failed for "
                  f"{len(nb_results) - success} notebook(s)", file=sys.stderr)
            return 1
        return _dry_run_candidate_probe()
    if args.plan:
        return cmd_plan(args)
    if args.apply:
        return cmd_apply(args)

    print("Error: no command specified. Use --plan, --apply, --resume, --cutover, "
          "--post-cutover-validate, --rollback, --clean-legacy, --finalize, "
          "--retire-legacy-sources, --purge-retained-legacy, --abort, --dry-run, "
          "or --inspect.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
