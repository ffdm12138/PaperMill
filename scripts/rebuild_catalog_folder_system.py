"""One-time replacement of retired generated indexes with Catalog folders."""
from __future__ import annotations
import argparse, json, os, shutil, uuid
from pathlib import Path
from config.settings import CATALOG_FOLDER_ROOT, DISCOVERY_KEYWORD_NOTEBOOK_DIR, PAPERS_DIR, PAPER_NUMBER_LEDGER_PATH
from src.catalog_folders.formal_registry import FormalPaperRegistry
from src.catalog_folders.reconcile import reconcile_catalog_folders
from src.catalog_folders.registry import now_iso, sync_registry
from src.catalog_folders.task_planner import plan_tasks
from src.library.paper_number_ledger import PaperNumberLedger
from src.utils.atomic_io import atomic_write_json

RETIRED=("all"+".catalog.json","paper_"+"index.json","index_"+"generation.json","generations")

def rebuild(*,catalog_root:Path,papers_dir:Path,ledger_path:Path,notebook_dir:Path,apply:bool)->dict:
    root=Path(catalog_root); retired=[root/name for name in RETIRED if (root/name).exists()]
    registry=FormalPaperRegistry(papers_dir=Path(papers_dir),ledger=PaperNumberLedger(ledger_path)); papers=registry.load(refresh=True)
    report={"formal_papers":len(papers),"retired_paths":[str(path) for path in retired],"applied":apply}
    if not apply:return report
    tx=root.parent/f".{root.name}.retired_{uuid.uuid4()}"; tx.mkdir(parents=True,exist_ok=False)
    try:
        for path in retired: os.replace(path,tx/path.name)
        sync_registry(notebook_dir=notebook_dir,registry_path=root/".state"/"category_registry.json",apply=True)
        report["reconcile"]=reconcile_catalog_folders(root=root,formal_registry=registry,apply=True)
        report["task_count"]=len(plan_tasks(root=root,formal_registry=registry,apply=True))
        shutil.rmtree(tx)
        atomic_write_json(root/".state"/"rebuild_receipt.json",{"schema_version":"1.0","rebuilt_at":now_iso(),"formal_papers":len(papers),"retired_paths_removed":[path.name for path in retired],"imported_retired_content":False},indent=2)
        return report
    except Exception:
        (root/".state").mkdir(parents=True,exist_ok=True); (root/".state"/"DIRTY").write_text("initial rebuild failed\n",encoding="utf-8"); raise

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("--catalog-root",type=Path,default=CATALOG_FOLDER_ROOT); p.add_argument("--papers-dir",type=Path,default=PAPERS_DIR); p.add_argument("--ledger-path",type=Path,default=PAPER_NUMBER_LEDGER_PATH); p.add_argument("--notebook-dir",type=Path,default=DISCOVERY_KEYWORD_NOTEBOOK_DIR); p.add_argument("--apply",action="store_true"); a=p.parse_args(argv); print(json.dumps(rebuild(catalog_root=a.catalog_root,papers_dir=a.papers_dir,ledger_path=a.ledger_path,notebook_dir=a.notebook_dir,apply=a.apply),ensure_ascii=False,indent=2)); return 0
if __name__=="__main__": raise SystemExit(main())
