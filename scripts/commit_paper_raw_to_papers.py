"""Commit formalized v2 paper_raw folders into data/papers (transactional install).

commit only accepts folders already formalized by formalize_paper_raw.py:
``.import_status.json status=ready_for_commit``, folder named <paper_id>,
``<paper_id>.formalization.json`` + ``<16-digit>.paper.number`` marker
present. commit does final validation + atomic install + all.catalog rebuild;
it never generates paper_id / paper_number / catalog links.
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
from src.services.ingest_state import READY_FOR_COMMIT, read_import_status
from src.services.ingest_ids import PAPER_NUMBER_RE
from src.services.v2_library import PaperNumberLedger, V2PaperCommitService


def _ready_dirs(root: Path, ledger_path: Path) -> list[Path]:
    out = []
    ledger = PaperNumberLedger(ledger_path)
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        name = folder.name
        if PAPER_NUMBER_RE.match(name):
            continue  # not yet formalized
        if read_import_status(folder).get("status") != READY_FOR_COMMIT:
            continue
        # formalize outputs: formalization.json + paper.number marker + 4 assets + images
        if not (folder / f"{name}.formalization.json").exists():
            continue
        if ledger.paper_number_from_marker(folder) is None:
            continue
        if all((folder / f"{name}.{suffix}").exists() for suffix in ("metadata.json", "catalog.json", "md", "pdf")) and (folder / "images").is_dir():
            out.append(folder)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Commit curated v2 paper_raw folders into data/papers.")
    parser.add_argument("--paper-dir", type=Path, default=None)
    parser.add_argument("--all-ready", action="store_true")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH,
                        help="paper_number_ledger.json path (tests/agents must pass a tmp path to avoid polluting data/catalog)")
    parser.add_argument("--all-catalog-path", type=Path, default=ALL_CATALOG_PATH,
                        help="all.catalog.json path (tests/agents must pass a tmp path to avoid polluting data/catalog)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    write = args.apply and not args.dry_run
    folders = [args.paper_dir] if args.paper_dir else _ready_dirs(args.paper_raw_dir, args.ledger_path) if args.all_ready else []
    if not folders:
        raise SystemExit("--paper-dir or --all-ready is required")
    service = V2PaperCommitService(
        papers_dir=args.papers_dir,
        all_catalog_path=args.all_catalog_path,
        ledger_path=args.ledger_path,
    )
    report = []
    for folder in folders:
        item = {"folder": str(folder), "status": "planned"}
        if write:
            try:
                result = service.commit_paper_raw(folder)
                item.update(result)
                item["status"] = result.get("status", "failed")
            except Exception as exc:
                item.update({"status": "failed", "error": str(exc)})
        report.append(item)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"applied": write, "items": report}, ensure_ascii=False, indent=2))
    return 1 if any(i["status"] not in {"planned", "imported"} for i in report) else 0


if __name__ == "__main__":
    raise SystemExit(main())
