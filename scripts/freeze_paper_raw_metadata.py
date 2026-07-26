"""Freeze citation-ready metadata after a verified PDF match."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)
from config.settings import PAPER_RAW_DIR
from src.metadata.freeze import freeze_metadata
from src.ingest.status import update_status
from src.ingest.workspace import PaperRawWorkspace
def main() -> int:
    p=argparse.ArgumentParser(); p.add_argument("--paper-number",required=True); p.add_argument("--paper-raw-dir",type=Path,default=PAPER_RAW_DIR); p.add_argument("--apply",action="store_true"); args=p.parse_args(); folder=args.paper_raw_dir/args.paper_number
    if not args.apply: print(json.dumps({"planned":str(folder/f"{args.paper_number}.metadata_freeze.json")},ensure_ascii=False)); return 0
    try:
        receipt=freeze_metadata(folder,args.paper_number)
        update_status(PaperRawWorkspace.from_path(folder),"metadata","frozen",revision=receipt["revision"])
        print(json.dumps(receipt,ensure_ascii=False,indent=2)); return 0
    except Exception as exc: print(f"ERROR: {exc}",file=sys.stderr); return 1
if __name__=="__main__": raise SystemExit(main())
