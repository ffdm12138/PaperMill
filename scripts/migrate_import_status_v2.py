"""Explicit dry-run-first migration from flat import status to nested v2."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent.parent))
from config.settings import PAPER_RAW_DIR
from src.ingest.status import migrate_flat_status
from src.utils.atomic_io import atomic_write_json

def main()->int:
    parser=argparse.ArgumentParser(); parser.add_argument("--paper-raw-dir",type=Path,default=PAPER_RAW_DIR); parser.add_argument("--apply",action="store_true"); parser.add_argument("--confirm-runtime-migration",action="store_true"); parser.add_argument("--report",type=Path); args=parser.parse_args()
    if args.apply and args.paper_raw_dir.resolve()==PAPER_RAW_DIR.resolve() and not args.confirm_runtime_migration: raise SystemExit("real runtime migration requires --confirm-runtime-migration")
    items=[]
    for folder in sorted(p for p in args.paper_raw_dir.iterdir() if p.is_dir() and len(p.name)==16 and p.name.isdigit()) if args.paper_raw_dir.exists() else []:
        path=folder/".import_status.json"
        if not path.exists(): continue
        old=json.loads(path.read_text(encoding="utf-8")); new=migrate_flat_status(old,folder.name); item={"paper_number":folder.name,"source_schema":old.get("schema_version","flat"),"target_schema":"2.0","status":"planned"}
        if args.apply: atomic_write_json(path,new,indent=2); item["status"]="migrated"
        items.append(item)
    out={"applied":args.apply,"items":items}
    if args.report: atomic_write_json(args.report,out,indent=2)
    print(json.dumps(out,ensure_ascii=False,indent=2)); return 0
if __name__=="__main__": raise SystemExit(main())
