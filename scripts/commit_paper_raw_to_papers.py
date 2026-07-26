"""Commit numeric paper_raw workspaces through external durable journals."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)
from config.settings import CATALOG_FOLDER_ROOT,PAPER_NUMBER_LEDGER_PATH,PAPER_RAW_DIR,PAPERS_DIR
from src.ingest.commit import commit_paper_raw
from src.ingest.commit_recovery import reconcile_commits
from src.ingest.status import inspect_workspace_readiness
from src.ingest.workspace import PAPER_NUMBER_RE,PaperRawWorkspace

def _candidates(root: Path,args)->list[PaperRawWorkspace]:
    if args.paper_dir: return [PaperRawWorkspace.from_path(args.paper_dir)]
    if args.paper_number: return [PaperRawWorkspace.open(root,args.paper_number)]
    if args.all_ready:
        out=[]
        for folder in sorted(p for p in root.iterdir() if p.is_dir() and PAPER_NUMBER_RE.fullmatch(p.name)):
            workspace=PaperRawWorkspace.from_path(folder)
            if inspect_workspace_readiness(workspace)["ready_for_commit"]: out.append(workspace)
        return out
    return []

def main()->int:
    parser=argparse.ArgumentParser(); parser.add_argument("--paper-dir",type=Path); parser.add_argument("--paper-number"); parser.add_argument("--all-ready",action="store_true"); parser.add_argument("--reconcile",action="store_true"); parser.add_argument("--paper-raw-dir",type=Path,default=PAPER_RAW_DIR); parser.add_argument("--papers-dir",type=Path,default=PAPERS_DIR); parser.add_argument("--ledger-path",type=Path,default=PAPER_NUMBER_LEDGER_PATH); parser.add_argument("--catalog-root",type=Path,default=CATALOG_FOLDER_ROOT); parser.add_argument("--transactions-dir",type=Path,default=None); parser.add_argument("--apply",action="store_true"); parser.add_argument("--dry-run",action="store_true"); parser.add_argument("--report",type=Path)
    args=parser.parse_args(); transactions=args.transactions_dir or args.paper_raw_dir.parent/"transactions"; write=args.apply and not args.dry_run; report=[]
    try:
        if args.reconcile:
            report=reconcile_commits(transactions_dir=transactions,paper_raw_root=args.paper_raw_dir,papers_dir=args.papers_dir,ledger_path=args.ledger_path,catalog_root=args.catalog_root,paper_number=args.paper_number,apply=write)
        else:
            workspaces=_candidates(args.paper_raw_dir,args)
            if not workspaces: raise ValueError("--paper-dir, --paper-number, --all-ready, or --reconcile is required")
            for workspace in workspaces:
                if write: report.append(commit_paper_raw(workspace,paper_raw_root=args.paper_raw_dir,papers_dir=args.papers_dir,ledger_path=args.ledger_path,catalog_root=args.catalog_root,transactions_dir=transactions))
                else: report.append({"status":"planned","paper_number":workspace.paper_number,"workspace":str(workspace.root)})
    except Exception as exc: report.append({"status":"failed","error":str(exc)})
    payload={"applied":write,"items":report};
    if args.report: args.report.parent.mkdir(parents=True,exist_ok=True); args.report.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(payload,ensure_ascii=False,indent=2)); return 1 if any(item.get("status")=="failed" for item in report) else 0
if __name__=="__main__": raise SystemExit(main())
