"""Validate a numeric paper_raw workspace and write an installation plan only."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import (
    PAPER_NUMBER_LEDGER_PATH,
    PAPER_RAW_DIR,
    PAPERS_DIR,
)
from src.services.ingest_state import CATALOG_READY, READY_FOR_COMMIT, read_import_status
from src.services.ingest_ids import PAPER_NUMBER_RE, validate_paper_raw_id
from src.ingest.formalization import write_formalization_plan
from src.ingest.workspace import PaperRawWorkspace


def _candidates(root: Path, args) -> list[Path]:
    if args.paper_dir:
        return [args.paper_dir]
    if args.paper_number:
        return [root / validate_paper_raw_id(args.paper_number)]
    if args.all_ready:
        out: list[Path] = []
        for folder in sorted(p for p in root.iterdir() if p.is_dir()):
            if folder.name in {"quarantine"}:
                continue
            status = read_import_status(folder).get("status")
            if status == CATALOG_READY:
                out.append(folder)
            elif status == READY_FOR_COMMIT and PAPER_NUMBER_RE.match(folder.name):
                out.append(folder)
        return out
    raise ValueError("--paper-dir, --paper-number, or --all-ready is required")


def main() -> int:
    parser = argparse.ArgumentParser(description="Formalize paper_raw into ready_for_commit state.")
    parser.add_argument("--paper-dir", type=Path, default=None)
    parser.add_argument("--paper-number", default=None)
    parser.add_argument("--all-ready", action="store_true")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH,
                        help="paper_number_ledger.json path (tests/agents must pass a tmp path to avoid polluting data/catalog)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    write = args.apply and not args.dry_run
    report = []
    for folder in _candidates(args.paper_raw_dir, args):
        item = {"folder": str(folder), "status": "planned"}
        try:
            if write:
                workspace = PaperRawWorkspace.from_path(folder)
                plan = write_formalization_plan(
                    workspace,
                    papers_dir=args.papers_dir,
                    ledger_path=args.ledger_path,
                )
                item.update({
                    "success": True,
                    "status": READY_FOR_COMMIT,
                    "paper_number": workspace.paper_number,
                    "paper_id": plan["paper_id"],
                    "folder": str(workspace.root),
                    "formalization": str(workspace.formalization),
                })
            else:
                from src.services.ingest_state import read_import_status as _ris

                status = _ris(folder).get("status")
                item["current_status"] = status
                item["status"] = "planned"
        except Exception as exc:
            item.update({"status": "failed", "error": str(exc)})
        report.append(item)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"applied": write, "items": report}, ensure_ascii=False, indent=2))
    ok_statuses = {"planned", READY_FOR_COMMIT, "prompt_generated"}
    return 1 if any(i.get("status") not in ok_statuses for i in report) else 0


if __name__ == "__main__":
    raise SystemExit(main())
