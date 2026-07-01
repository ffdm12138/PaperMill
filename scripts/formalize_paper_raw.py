"""Formalize paper_raw into ready_for_commit state (rename, reserve number, backfill catalog).

This is the mandatory step between ``curate_paper_raw.py`` (which leaves the
folder at ``status=catalog_ready`` with a 6-digit source_id) and
``commit_paper_raw_to_papers.py`` (which only accepts ``ready_for_commit``
folders already renamed to ``<paper_id>`` with a ``<16-digit>.paper.number``
marker and a ``<paper_id>.formalization.json`` manifest).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import (
    ALL_CATALOG_PATH,
    PAPER_NUMBER_LEDGER_PATH,
    PAPER_RAW_DIR,
    PAPERS_DIR,
)
from src.services.ingest_state import CATALOG_READY, READY_FOR_COMMIT, read_import_status
from src.services.paper_raw_formalizer import PaperRawFormalizationService
from src.services.v2_library import _TEMP_ID_RE


def _candidates(root: Path, args) -> list[Path]:
    if args.paper_dir:
        return [args.paper_dir]
    if args.source_id:
        return [root / args.source_id]
    if args.all_ready:
        out: list[Path] = []
        for folder in sorted(p for p in root.iterdir() if p.is_dir()):
            if folder.name in {"quarantine"}:
                continue
            status = read_import_status(folder).get("status")
            if status == CATALOG_READY:
                out.append(folder)
            elif status == READY_FOR_COMMIT and not _TEMP_ID_RE.match(folder.name):
                # already formalized; include so re-runs are idempotent (no-op)
                out.append(folder)
        return out
    raise ValueError("--paper-dir, --source-id, or --all-ready is required")


def main() -> int:
    parser = argparse.ArgumentParser(description="Formalize paper_raw into ready_for_commit state.")
    parser.add_argument("--paper-dir", type=Path, default=None)
    parser.add_argument("--source-id", default=None)
    parser.add_argument("--all-ready", action="store_true")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH,
                        help="paper_number_ledger.json path (tests/agents must pass a tmp path to avoid polluting data/catalog)")
    parser.add_argument("--all-catalog-path", type=Path, default=ALL_CATALOG_PATH,
                        help="all.catalog.json path (tests/agents must pass a tmp path to avoid polluting data/catalog)")
    parser.add_argument("--paper-id", default=None)
    parser.add_argument("--preserve-paper-number", default=None, help="reuse this 16-digit number instead of reserving a new one")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    write = args.apply and not args.dry_run
    service = PaperRawFormalizationService(
        paper_raw_dir=args.paper_raw_dir,
        papers_dir=args.papers_dir,
        ledger_path=args.ledger_path,
        all_catalog_path=args.all_catalog_path,
    )
    report = []
    for folder in _candidates(args.paper_raw_dir, args):
        item = {"folder": str(folder), "status": "planned"}
        try:
            if write:
                result = service.formalize(
                    folder,
                    paper_id=args.paper_id,
                    preserve_paper_number=args.preserve_paper_number,
                )
                item.update(result)
                if not result.get("success"):
                    item["status"] = "failed"
                elif result.get("status") == READY_FOR_COMMIT:
                    item["status"] = READY_FOR_COMMIT
                else:
                    item["status"] = result.get("status") or "failed"
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
