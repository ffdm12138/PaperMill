"""Prepare a read-only Catalog Skill task envelope before catalog generation."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)
from config.settings import PAPER_RAW_DIR
from src.catalog.task import write_task_envelope
from src.ingest.status import update_status
from src.ingest.workspace import PaperRawWorkspace
def main():
 p=argparse.ArgumentParser(); p.add_argument("--paper-number",required=True); p.add_argument("--paper-raw-dir",type=Path,default=PAPER_RAW_DIR); p.add_argument("--apply",action="store_true"); a=p.parse_args(); f=a.paper_raw_dir/a.paper_number
 if not a.apply: print(json.dumps({"planned":str(f/f'{a.paper_number}.catalog_task.json')},ensure_ascii=False)); return 0
 try:
  path=write_task_envelope(f,a.paper_number); update_status(PaperRawWorkspace.from_path(f),"catalog","waiting_for_llm",task=str(path.name)); print(json.dumps({"task":str(path)},ensure_ascii=False)); return 0
 except Exception as e: print(f"ERROR: {e}",file=sys.stderr); return 1
if __name__=="__main__": raise SystemExit(main())
