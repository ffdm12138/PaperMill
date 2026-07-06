"""Admin-only reset/compact operations for paper_number ledger."""
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
    parser = argparse.ArgumentParser(description="Admin-only paper_number ledger reset/compact tool.")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--transactions-dir", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true", help="force dry-run (default)")
    parser.add_argument("--apply", action="store_true", help="write changes")
    parser.add_argument("--i-understand-this-rewrites-paper-numbers", action="store_true")
    parser.add_argument("--reason", default="")
    parser.add_argument("--purge-empty-invalid", action="store_true")
    parser.add_argument("--protect-metadata", action="store_true",
                        help="compact only: fingerprint metadata before/after and roll back if bibliographic fields change")
    parser.add_argument("--sort", choices=["old-number", "year"], default="old-number")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--audit", action="store_true", help="run audit only")
    mode.add_argument("--reset-empty", action="store_true")
    mode.add_argument("--compact-paper-raw", action="store_true")
    args = parser.parse_args()

    write = bool(args.apply and not args.dry_run)
    if args.apply and not args.i_understand_this_rewrites_paper_numbers:
        parser.error("--apply requires --i-understand-this-rewrites-paper-numbers")
    if write and not args.reason.strip():
        parser.error("--apply requires --reason")
    if args.purge_empty_invalid and not write:
        parser.error("--purge-empty-invalid is only allowed with --apply")

    service = PaperNumberAdminService(
        paper_raw_dir=args.paper_raw_dir,
        papers_dir=args.papers_dir,
        ledger_path=args.ledger_path,
        transactions_dir=args.transactions_dir,
    )
    if args.audit:
        report = service.audit(strict=False)
    elif args.reset_empty:
        report = service.reset_empty(
            apply=write,
            reason=args.reason,
            purge_empty_invalid=args.purge_empty_invalid,
        )
    else:
        report = service.compact_paper_raw(
            apply=write,
            reason=args.reason,
            sort=args.sort,
            purge_empty_invalid=args.purge_empty_invalid,
            protect_metadata=args.protect_metadata,
        )

    _write_report(args.report, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report.get("errors") or (report.get("post_audit") and not report["post_audit"].get("ok")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
