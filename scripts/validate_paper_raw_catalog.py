"""Validate a frozen-metadata catalog v3.2 without modifying its contents."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent.parent))
from config.settings import PAPER_RAW_DIR
from src.catalog.schema import validate_catalog_v32
from src.catalog.freeze import freeze_catalog
from src.ingest.status import update_status
from src.ingest.workspace import PaperRawWorkspace
def main()->int:
 p=argparse.ArgumentParser(); p.add_argument("--paper-number",required=True); p.add_argument("--paper-raw-dir",type=Path,default=PAPER_RAW_DIR); args=p.parse_args(); f=args.paper_raw_dir/args.paper_number
 try: cat=json.loads((f/f"{args.paper_number}.catalog.json").read_text(encoding="utf-8")); errors=validate_catalog_v32(cat,f,args.paper_number)
 except Exception as exc: errors=[str(exc)]
 if not errors:
  try:
   receipt=freeze_catalog(f,args.paper_number,paper_raw_root=args.paper_raw_dir); update_status(PaperRawWorkspace.from_path(f),"catalog","frozen",catalog_sha256=receipt["catalog_sha256"],paper_name=receipt["paper_name"])
  except Exception as exc: errors.append(f"catalog freeze: {exc}")
 print(json.dumps({"paper_number":args.paper_number,"valid":not errors,"errors":errors},ensure_ascii=False,indent=2)); return 0 if not errors else 1
if __name__=="__main__": raise SystemExit(main())
