"""Validate the ledger-backed formal library and Catalog folder view."""
from __future__ import annotations
import argparse, json
from pathlib import Path
from config.settings import CATALOG_FOLDER_ROOT, PAPERS_DIR, PAPER_NUMBER_LEDGER_PATH
from src.catalog_folders.formal_registry import FormalPaperRegistry
from src.catalog_folders.validation import doctor
from src.library.paper_number_ledger import PaperNumberLedger

def validate_v2_library(*, papers_dir: Path = PAPERS_DIR, ledger_path: Path = PAPER_NUMBER_LEDGER_PATH, catalog_root: Path = CATALOG_FOLDER_ROOT) -> dict:
    registry=FormalPaperRegistry(papers_dir=Path(papers_dir),ledger=PaperNumberLedger(ledger_path)); report=doctor(root=Path(catalog_root),formal_registry=registry); return {"valid":report["writer_safe"],**report}

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("--papers-dir",type=Path,default=PAPERS_DIR); p.add_argument("--ledger-path",type=Path,default=PAPER_NUMBER_LEDGER_PATH); p.add_argument("--catalog-root",type=Path,default=CATALOG_FOLDER_ROOT); a=p.parse_args(argv); report=validate_v2_library(papers_dir=a.papers_dir,ledger_path=a.ledger_path,catalog_root=a.catalog_root); print(json.dumps(report,ensure_ascii=False,indent=2)); return 0 if report["valid"] else 1
if __name__=="__main__": raise SystemExit(main())
