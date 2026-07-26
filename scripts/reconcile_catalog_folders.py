from __future__ import annotations
import argparse, json
from pathlib import Path
from config.settings import CATALOG_FOLDER_ROOT, PAPERS_DIR, PAPER_NUMBER_LEDGER_PATH
from src.catalog_folders.formal_registry import FormalPaperRegistry
from src.catalog_folders.reconcile import reconcile_catalog_folders
from src.library.paper_number_ledger import PaperNumberLedger
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("--catalog-root",type=Path,default=CATALOG_FOLDER_ROOT); p.add_argument("--papers-dir",type=Path,default=PAPERS_DIR); p.add_argument("--ledger-path",type=Path,default=PAPER_NUMBER_LEDGER_PATH); p.add_argument("--apply",action="store_true"); a=p.parse_args(argv)
    registry=FormalPaperRegistry(papers_dir=a.papers_dir,ledger=PaperNumberLedger(a.ledger_path)); print(json.dumps(reconcile_catalog_folders(root=a.catalog_root,formal_registry=registry,apply=a.apply),ensure_ascii=False,indent=2)); return 0
if __name__=="__main__": raise SystemExit(main())
