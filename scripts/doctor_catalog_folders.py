from __future__ import annotations
import argparse, json, sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)
from config.settings import (
    CATALOG_FOLDER_ROOT, TRANSACTION_ROOT,
    PAPERS_DIR, PAPER_NUMBER_LEDGER_PATH,
)
from src.catalog_folders.formal_registry import FormalPaperRegistry
from src.catalog_folders.validation import doctor
from src.discovery.runtime_context import (
    DiscoveryRuntimeUnavailableError,
    resolve_active_runtime,
)
from src.library.paper_number_ledger import PaperNumberLedger

def main(argv=None):
    p=argparse.ArgumentParser(
        description="Diagnose catalog folder state. "
        "Exit 0 = complete/safe for writer; 1 = repairable incomplete/dirty; 2 = structural damage."
    )
    p.add_argument("--catalog-root",type=Path,default=CATALOG_FOLDER_ROOT)
    p.add_argument("--papers-dir",type=Path,default=PAPERS_DIR)
    p.add_argument("--ledger-path",type=Path,default=PAPER_NUMBER_LEDGER_PATH)
    p.add_argument("--workspace-root",type=Path,default=None,
                   help="Override the active discovery workspace root (for tests/staging).")
    p.add_argument("--transaction-root",type=Path,default=TRANSACTION_ROOT)
    p.add_argument("--json",action="store_true",help="Machine-readable JSON output")
    p.add_argument("--allow-empty-categories",action="store_true",
                   help="Suppress empty-category error (init/testing only)")
    a=p.parse_args(argv)

    try:
        ctx = resolve_active_runtime(workspace_root=a.workspace_root)
    except DiscoveryRuntimeUnavailableError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    report=doctor(
        root=a.catalog_root,
        formal_registry=FormalPaperRegistry(
            papers_dir=a.papers_dir,
            ledger=PaperNumberLedger(a.ledger_path),
        ),
        notebook_dir=ctx.notebook_root,
        transaction_root=a.transaction_root,
        allow_empty_categories=a.allow_empty_categories,
    )

    if a.json:
        print(json.dumps(report,ensure_ascii=False,indent=2))
    else:
        print(f"Catalog root:          {a.catalog_root}")
        print(f"Formal paper count:    {report['active_formal_papers']}")
        print(f"Category count:        {report['categories']}")
        print(f"All member count:      {report['all_members']}")
        print(f"Pending paper count:   {report['pending']}")
        print(f"Missing decision count:{report['missing_decisions']}")
        print(f"Stale decision count:  {report['stale_decisions']}")
        print(f"Classification tasks:  {report['classification_tasks']}")
        print(f"Unapplied results:     {report['unapplied_results']}")
        print(f"Broken link count:     {report['broken_links']}")
        print(f"Escaping link count:   {report['escaping_links']}")
        print(f"Unknown directory count:{report['unknown_directories']}")
        print(f"DIRTY state:           {report['dirty']}")
        print(f"Folder integrity safe: {report['folder_integrity_safe']}")
        print(f"Classification complete:{report['classification_complete']}")
        print(f"Writer category safe:  {report['writer_category_safe']}")
        print(f"Notebook schema safe:  {report['notebook_schema_safe']}")
        print(f"Discovery query ready: {report['discovery_query_ready']}")
        if report["discovery_query_errors"]:
            print(f"\nDiscovery query errors ({len(report['discovery_query_errors'])}):")
            for err in report["discovery_query_errors"]:
                print(f"  - {err}")
        if report["errors"]:
            print(f"\nErrors ({len(report['errors'])}):")
            for err in report["errors"]:
                print(f"  - {err}")

    # Exit codes: 0 = complete/safe, 1 = incomplete/dirty, 2 = structural damage
    if report["writer_category_safe"]:
        return 0
    if report["folder_integrity_safe"]:
        return 1
    return 2

if __name__=="__main__": raise SystemExit(main())
