from __future__ import annotations
import argparse, json
from pathlib import Path
from config.settings import CATALOG_FOLDER_ROOT, PAPERS_DIR, PAPER_NUMBER_LEDGER_PATH
from src.catalog_folders.formal_registry import FormalPaperRegistry
from src.catalog_folders.result_validator import apply_result
from src.catalog_folders.reconcile import reconcile_catalog_folders
from src.library.paper_number_ledger import PaperNumberLedger

def main(argv=None):
    p=argparse.ArgumentParser(
        description="Validate and apply a single classification result JSON, "
        "then reconcile category folder links."
    )
    p.add_argument("--result",type=Path,required=True,help="Path to classification result JSON")
    p.add_argument("--catalog-root",type=Path,default=CATALOG_FOLDER_ROOT)
    p.add_argument("--papers-dir",type=Path,default=PAPERS_DIR)
    p.add_argument("--ledger-path",type=Path,default=PAPER_NUMBER_LEDGER_PATH)
    p.add_argument("--apply",action="store_true",help="Write assignment and receipt to disk")
    p.add_argument("--force",action="store_true",
                   help="Replace stale decisions even when Catalog hash differs from task")
    a=p.parse_args(argv)
    registry=FormalPaperRegistry(papers_dir=a.papers_dir,ledger=PaperNumberLedger(a.ledger_path))
    applied=apply_result(
        result_path=a.result, root=a.catalog_root,
        formal_registry=registry, apply=a.apply, force=a.force,
    )
    if applied.get("status") == "already_applied":
        print(json.dumps(applied,ensure_ascii=False,indent=2))
        return 0
    reconcile_catalog_folders(root=a.catalog_root,formal_registry=registry,apply=a.apply)
    print(json.dumps(applied,ensure_ascii=False,indent=2))
    return 0
if __name__=="__main__": raise SystemExit(main())
