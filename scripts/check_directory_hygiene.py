"""Repository and Catalog-folder hygiene checks."""
from __future__ import annotations
import argparse, json
from pathlib import Path
from config.settings import CATALOG_FOLDER_ROOT, PAPER_RAW_DIR, PAPERS_DIR, PAPER_NUMBER_LEDGER_PATH, PROJECT_ROOT
from src.catalog_folders.formal_registry import FormalPaperRegistry
from src.catalog_folders.validation import doctor
from src.library.paper_number_ledger import PaperNumberLedger
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

def check_directory_hygiene(*, project_root: Path = PROJECT_ROOT, paper_raw_dir: Path = PAPER_RAW_DIR, papers_dir: Path = PAPERS_DIR, catalog_root: Path = CATALOG_FOLDER_ROOT, ledger_path: Path = PAPER_NUMBER_LEDGER_PATH) -> dict:
    del paper_raw_dir
    errors=[]; warnings=[]
    for retired in ("all"+".catalog.json","paper_"+"index.json","index_"+"generation.json","generations"):
        if (Path(catalog_root)/retired).exists(): errors.append(f"retired Catalog index path exists: {retired}")
    report=doctor(root=Path(catalog_root),formal_registry=FormalPaperRegistry(papers_dir=Path(papers_dir),ledger=PaperNumberLedger(ledger_path)))
    errors.extend(report["errors"])
    return {"ok":not errors,"errors":errors,"warnings":warnings,"catalog_folders":report}

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("--project-root",type=Path,default=PROJECT_ROOT); p.add_argument("--papers-dir",type=Path,default=PAPERS_DIR); p.add_argument("--catalog-root",type=Path,default=CATALOG_FOLDER_ROOT); p.add_argument("--ledger-path",type=Path,default=PAPER_NUMBER_LEDGER_PATH); a=p.parse_args(argv); report=check_directory_hygiene(project_root=a.project_root,papers_dir=a.papers_dir,catalog_root=a.catalog_root,ledger_path=a.ledger_path); print(json.dumps(report,ensure_ascii=False,indent=2)); return 0 if report["ok"] else 1
if __name__=="__main__": raise SystemExit(main())
