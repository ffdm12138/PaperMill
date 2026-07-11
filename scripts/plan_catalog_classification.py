from __future__ import annotations
import argparse, json
from pathlib import Path
from config.settings import CATALOG_FOLDER_ROOT, PAPERS_DIR, PAPER_NUMBER_LEDGER_PATH
from src.catalog_folders.formal_registry import FormalPaperRegistry
from src.catalog_folders.task_planner import plan_tasks
from src.library.paper_number_ledger import PaperNumberLedger

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("--catalog-root",type=Path,default=CATALOG_FOLDER_ROOT); p.add_argument("--papers-dir",type=Path,default=PAPERS_DIR); p.add_argument("--ledger-path",type=Path,default=PAPER_NUMBER_LEDGER_PATH); p.add_argument("--paper-number"); p.add_argument("--category-id"); p.add_argument("--all",action="store_true"); p.add_argument("--apply",action="store_true"); a=p.parse_args(argv)
    if not (a.all or a.paper_number or a.category_id): p.error("choose --all, --paper-number, or --category-id")
    registry=FormalPaperRegistry(papers_dir=a.papers_dir,ledger=PaperNumberLedger(a.ledger_path)); tasks=plan_tasks(root=a.catalog_root,formal_registry=registry,paper_number=a.paper_number,category_id=a.category_id,apply=a.apply); print(json.dumps({"task_count":len(tasks),"tasks":tasks},ensure_ascii=False,indent=2)); return 0
if __name__=="__main__": raise SystemExit(main())
