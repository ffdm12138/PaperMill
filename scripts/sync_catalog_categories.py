from __future__ import annotations
import argparse, json
from pathlib import Path
from config.settings import CATALOG_FOLDER_ROOT, DISCOVERY_KEYWORD_NOTEBOOK_DIR
from src.catalog_folders.registry import sync_registry

def main(argv=None):
    parser=argparse.ArgumentParser(); parser.add_argument("--catalog-root",type=Path,default=CATALOG_FOLDER_ROOT); parser.add_argument("--notebook-dir",type=Path,default=DISCOVERY_KEYWORD_NOTEBOOK_DIR); parser.add_argument("--apply",action="store_true")
    args=parser.parse_args(argv); report=sync_registry(notebook_dir=args.notebook_dir,registry_path=args.catalog_root/".state"/"category_registry.json",apply=args.apply); print(json.dumps(report,ensure_ascii=False,indent=2)); return 0
if __name__=="__main__": raise SystemExit(main())
