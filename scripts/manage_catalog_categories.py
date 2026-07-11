from __future__ import annotations
import argparse, json
from pathlib import Path
from config.settings import CATALOG_FOLDER_ROOT
from src.catalog_folders.registry import load_registry, now_iso
from src.utils.atomic_io import atomic_write_json

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("command",choices=["list","retire"]); p.add_argument("--category-id"); p.add_argument("--catalog-root",type=Path,default=CATALOG_FOLDER_ROOT); p.add_argument("--apply",action="store_true"); a=p.parse_args(argv); path=a.catalog_root/".state"/"category_registry.json"; data=load_registry(path)
    if a.command=="retire":
        if not a.category_id:p.error("--category-id is required for retire")
        row=next((row for row in data["categories"] if row.get("category_id")==a.category_id),None)
        if row is None:raise SystemExit(f"unknown category: {a.category_id}")
        row["classification_enabled"]=False; row["retired_at"]=now_iso(); data["updated_at"]=now_iso()
        if a.apply:atomic_write_json(path,data,indent=2)
    print(json.dumps(data,ensure_ascii=False,indent=2)); return 0
if __name__=="__main__": raise SystemExit(main())
