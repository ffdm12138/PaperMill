"""Prepare or validate/freeze Catalog v3.2 in numeric raw workspaces."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent.parent))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)
from config.settings import PAPER_RAW_DIR,PAPERS_DIR
from src.catalog.freeze import freeze_catalog
from src.catalog.task import write_task_envelope
from src.ingest.status import update_status
from src.ingest.workspace import PAPER_NUMBER_RE,PaperRawWorkspace
from src.metadata.freeze import assert_metadata_frozen
from src.utils.atomic_io import atomic_write_json

def _candidates(root: Path,args)->list[PaperRawWorkspace]:
    if args.paper_dir: return [PaperRawWorkspace.from_path(args.paper_dir)]
    if args.paper_number: return [PaperRawWorkspace.open(root,args.paper_number)]
    if args.all_ready or args.all_matched:
        out=[]
        for folder in sorted(p for p in root.iterdir() if p.is_dir() and PAPER_NUMBER_RE.fullmatch(p.name)):
            workspace=PaperRawWorkspace.from_path(folder)
            try: assert_metadata_frozen(folder,workspace.paper_number)
            except Exception: continue
            if workspace.markdown.is_file() and workspace.conversion.is_file() and workspace.images.is_dir():
                if not args.apply or workspace.catalog.is_file(): out.append(workspace)
        return out
    raise ValueError("--paper-dir, --paper-number, --all-ready, or --all-matched is required")

def main()->int:
    parser=argparse.ArgumentParser(); parser.add_argument("--paper-dir",type=Path); parser.add_argument("--paper-number"); parser.add_argument("--all-ready",action="store_true"); parser.add_argument("--all-matched",action="store_true"); parser.add_argument("--paper-raw-dir",type=Path,default=PAPER_RAW_DIR); parser.add_argument("--papers-dir",type=Path,default=PAPERS_DIR); parser.add_argument("--catalog",type=Path); parser.add_argument("--apply",action="store_true"); parser.add_argument("--dry-run",action="store_true"); parser.add_argument("--report",type=Path); args=parser.parse_args(); write=args.apply and not args.dry_run; report=[]
    for workspace in _candidates(args.paper_raw_dir,args):
        item={"paper_number":workspace.paper_number,"workspace":str(workspace.root)}
        try:
            if write:
                if not workspace.catalog_task.exists(): write_task_envelope(workspace.root,workspace.paper_number)
                if args.catalog and args.catalog.resolve()!=workspace.catalog.resolve(): atomic_write_json(workspace.catalog,json.loads(args.catalog.read_text(encoding="utf-8")),indent=2)
                receipt=freeze_catalog(workspace.root,workspace.paper_number,papers_dir=args.papers_dir,paper_raw_root=args.paper_raw_dir); update_status(workspace,"catalog","frozen",paper_name=receipt["paper_name"],catalog_sha256=receipt["catalog_sha256"]); item.update({"status":"catalog_frozen","paper_name":receipt["paper_name"]})
            else: item.update({"status":"planned","task":str(workspace.catalog_task),"task_exists":workspace.catalog_task.exists()})
        except Exception as exc: item.update({"status":"failed","error":str(exc)})
        report.append(item)
    if args.report: atomic_write_json(args.report,{"applied":write,"items":report},indent=2)
    print(json.dumps({"applied":write,"items":report},ensure_ascii=False,indent=2)); return 1 if any(x["status"]=="failed" for x in report) else 0
if __name__=="__main__": raise SystemExit(main())
