"""Sync category registry from DOI keyword notebooks and reconcile folders.

Default behaviour (no flags): read notebooks and report the registry/folder/task
plan without writing.  Pass --apply to update the registry, folders, and tasks.

Use --registry-only to only update the registry file without touching folders.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)
from config.settings import CATALOG_FOLDER_ROOT, PAPERS_DIR, PAPER_NUMBER_LEDGER_PATH
from src.catalog_folders.formal_registry import FormalPaperRegistry
from src.catalog_folders.reconcile import reconcile_catalog_folders
from src.catalog_folders.registry import sync_registry
from src.catalog_folders.task_planner import plan_tasks
from src.discovery.runtime_context import (
    DiscoveryRuntimeUnavailableError,
    resolve_active_runtime,
)
from src.library.paper_number_ledger import PaperNumberLedger


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Sync category registry from DOI keyword notebooks and reconcile folders."
    )
    parser.add_argument("--catalog-root", type=Path, default=CATALOG_FOLDER_ROOT)
    parser.add_argument("--workspace-root", type=Path, default=None,
                        help="Override the active discovery workspace root (for tests/staging).")
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--apply", action="store_true",
                        help="Write changes to disk")
    parser.add_argument("--registry-only", action="store_true",
                        help="Only update the registry file; skip folders, tasks, and reconcile")
    args = parser.parse_args(argv)

    try:
        ctx = resolve_active_runtime(workspace_root=args.workspace_root)
    except DiscoveryRuntimeUnavailableError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    registry_path = args.catalog_root / ".state" / "category_registry.json"

    # Step 1: sync registry from notebooks (this overwrites the file)
    reg_report = sync_registry(
        notebook_dir=ctx.notebook_root,
        registry_path=registry_path,
        apply=args.apply,
    )
    added = reg_report["added"]
    changed = reg_report["changed"]
    retired = reg_report.get("retired", [])
    collisions = reg_report.get("collisions", [])
    notebook_parse_errors = reg_report.get("notebook_parse_errors", [])
    invalid_keywords = reg_report.get("invalid_keywords", [])

    print(f"Categories added:     {len(added)}")
    print(f"Categories updated:   {len(changed)}")
    print(f"Categories retired:   {len(retired)}")
    if notebook_parse_errors:
        print(f"Notebook parse errors: {len(notebook_parse_errors)}")
        for pe in notebook_parse_errors[:5]:
            print(f"  - {pe['path']}: {pe['error']}")
    if invalid_keywords:
        print(f"Invalid keywords:     {len(invalid_keywords)}")
        for ik in invalid_keywords[:5]:
            print(f"  - {ik['keyword']!r} ({ik['path']}): {ik['error']}")
    if collisions:
        print(f"Keyword collisions:   {len(collisions)}")
        for c in collisions:
            if c.get("type") == "same_keyword_different_id":
                print(f"  - keyword={c['keyword']} ids={c['ids']}")
            elif c.get("type") == "same_id_different_keyword":
                print(f"  - id={c['keyword_id']} keywords={c['keywords']}")

    has_errors = bool(notebook_parse_errors or invalid_keywords or collisions)
    if has_errors:
        print(f"\n[BLOCKED] {len(notebook_parse_errors) + len(invalid_keywords) + len(collisions)} error(s) prevent registry apply. "
              f"Fix all notebook errors before retrying --apply.")
        if args.apply:
            return 1

    if args.registry_only:
        if args.apply:
            print("Registry updated (--registry-only). Folders, tasks, and reconcile skipped.")
        else:
            print("Dry run — pass --apply to write.")
        return 0

    # Step 2: reconcile folders
    registry = FormalPaperRegistry(
        papers_dir=args.papers_dir,
        ledger=PaperNumberLedger(args.ledger_path),
    )
    rec_report = reconcile_catalog_folders(
        root=args.catalog_root,
        formal_registry=registry,
        apply=args.apply,
    )
    print(f"Folders reconciled:  yes" if args.apply else "Folders reconciled:  dry-run")

    # Step 3: plan missing tasks
    tasks = plan_tasks(
        root=args.catalog_root,
        formal_registry=registry,
        apply=args.apply,
    )
    print(f"Tasks planned:       {len(tasks)}")
    print(f"Papers pending:      {rec_report.get('pending_count', '?')}")

    if not args.apply:
        print("\nDry run — pass --apply to write registry, folders, and tasks.")
    elif len(tasks) > 0:
        if len(added) > 0:
            print(f"\nNew Chinese categories added: {len(added)}")
            print(f"Papers needing classification: {rec_report.get('pending_count', '?')}")
        print(f"\nClassification tasks ready: {len(tasks)}")
        print(f"To classify, run the catalog-folder-classifier skill manually:")
        print(f"  python scripts/claim_catalog_classification_tasks.py --worker codex --max-tasks 10")
        print(f"  (Read each task's Catalog, produce real judgments, apply results)")
        print(f"\nDo NOT use --backend fake on real data.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
