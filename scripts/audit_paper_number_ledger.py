"""Audit paper_number ledger/workspace consistency."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import PAPER_NUMBER_LEDGER_PATH, PAPER_RAW_DIR, PAPERS_DIR
from src.services.paper_number_admin import PaperNumberAdminService


def _write_report(path: Path | None, report: dict) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit paper_number ledger and paper_raw workspaces.")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--strict", action="store_true", help="return non-zero if any mismatch is found")
    parser.add_argument("--expect-count", type=int, default=None,
                        help="additionally require exactly N active workspaces numbered 1..N with ledger max=N")
    args = parser.parse_args()

    service = PaperNumberAdminService(
        paper_raw_dir=args.paper_raw_dir,
        papers_dir=args.papers_dir,
        ledger_path=args.ledger_path,
    )
    report = service.audit(strict=args.strict, expect_count=args.expect_count)
    _write_report(args.report, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.strict and not report.get("ok") else 0


if __name__ == "__main__":
    raise SystemExit(main())
