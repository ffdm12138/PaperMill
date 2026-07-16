"""Run discovery for one Chinese classification notebook.

The CLI selects a schema-v3 notebook by ``keyword_zh``.  It never accepts a
free-standing provider query: every active Chinese and English search query is
read from the selected notebook and sent to both OpenAlex and Crossref.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (  # noqa: E402
    DISCOVERY_KEYWORD_NOTEBOOK_DIR,
    DISCOVERY_DIR,
    DISCOVERY_EXPORTS_DIR,
    DISCOVERY_LOCKS_DIR,
    DISCOVERY_PENDING_PAGES_DIR,
    PAPER_NUMBER_LEDGER_PATH,
    PAPER_RAW_DIR,
    PAPERS_DIR,
)
from src.discovery.cli_plan import load_keyword_plan  # noqa: E402
from src.discovery.coordinator import DiscoveryOptions, run_discovery_batch  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover papers from all active queries in one Chinese keyword notebook.",
        epilog=(
            "Use manage_discovery_keywords.py to curate notebook queries. "
            "English queries improve retrieval but never create Catalog categories. "
            "For 3--8 notebooks use discover_papers_concurrent.py with 3--4 workers; "
            "paper allocation remains protected by .paper_raw_write.lock."
        ),
    )
    parser.add_argument(
        "--keyword-zh",
        required=True,
        help="Exact Chinese classification keyword of an existing schema-v3 notebook.",
    )
    parser.add_argument("--mode", choices=["hybrid", "refresh", "backfill"], default="hybrid")
    parser.add_argument("--refresh-pages", type=int, default=2)
    parser.add_argument("--backfill-pages", type=int, default=5)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--max-candidates", type=int, default=50)
    parser.add_argument("--output-dir", type=Path, default=DISCOVERY_DIR / "doi_candidates")
    parser.add_argument("--keyword-notebook-dir", type=Path, default=DISCOVERY_KEYWORD_NOTEBOOK_DIR)
    parser.add_argument("--pending-pages-dir", type=Path, default=DISCOVERY_PENDING_PAGES_DIR)
    parser.add_argument("--discovery-locks-dir", type=Path, default=DISCOVERY_LOCKS_DIR)
    parser.add_argument("--exports-dir", type=Path, default=DISCOVERY_EXPORTS_DIR)
    parser.add_argument("--until-exhausted", action="store_true")
    parser.add_argument("--max-pages-total", type=int, default=None)
    parser.add_argument("--max-pending-candidates", type=int, default=1000)
    parser.add_argument("--resume-pending-candidates", type=int, default=700)
    parser.add_argument("--doi-resolution-budget", type=int, default=10)
    parser.add_argument("--stage-to-paper-raw", action="store_true")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--skip-duplicates", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and validate the notebook, print every provider lane, and make no writes or requests.",
    )
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--hide-existing", action="store_true")
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.page_size < 1:
        parser.error("--page-size must be >= 1")
    if args.refresh_pages < 1:
        parser.error("--refresh-pages must be >= 1")
    if args.backfill_pages < 1:
        parser.error("--backfill-pages must be >= 1")
    if args.max_candidates < 1:
        parser.error("--max-candidates must be >= 1")
    if args.stage_to_paper_raw and not args.apply and not args.dry_run:
        parser.error("--stage-to-paper-raw requires --apply")
    if args.apply and not args.stage_to_paper_raw:
        parser.error("--apply requires --stage-to-paper-raw")
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")
    if args.skip_duplicates and not args.stage_to_paper_raw:
        parser.error("--skip-duplicates requires --stage-to-paper-raw")
    if args.until_exhausted and args.mode not in ("backfill", "hybrid"):
        parser.error("--until-exhausted requires --mode backfill or hybrid")
    if args.until_exhausted and args.max_pages_total is None:
        parser.error("--until-exhausted requires explicit --max-pages-total")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)

    try:
        plan = load_keyword_plan(
            args.keyword_zh,
            args.keyword_notebook_dir,
            mode=args.mode,
            refresh_pages=args.refresh_pages,
            backfill_pages=args.backfill_pages,
            max_workers=2,
            max_pages_total=args.max_pages_total,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[ERROR] discovery preflight failed: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print("[DRY-RUN] validated read-only discovery plan")
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if plan["enabled"] is False:
        print(f"[SKIP] disabled notebook: {args.keyword_zh}")
        return 0

    options = DiscoveryOptions(
        mode=args.mode,
        refresh_pages=args.refresh_pages,
        backfill_pages=args.backfill_pages,
        page_size=args.page_size,
        max_candidates=args.max_candidates,
        output_dir=args.output_dir,
        paper_raw_dir=args.paper_raw_dir,
        papers_dir=args.papers_dir,
        ledger_path=args.ledger_path,
        stage_to_paper_raw=args.stage_to_paper_raw,
        apply=args.apply,
        skip_duplicates=args.skip_duplicates,
        hide_existing=args.hide_existing,
        notebook_dir=args.keyword_notebook_dir,
        pending_pages_dir=args.pending_pages_dir,
        locks_dir=args.discovery_locks_dir,
        exports_dir=args.exports_dir,
        until_exhausted=args.until_exhausted,
        max_pages_total=args.max_pages_total,
        doi_resolution_budget=args.doi_resolution_budget,
        max_pending_candidates=args.max_pending_candidates,
        resume_pending_candidates=args.resume_pending_candidates,
    )
    batch_report = run_discovery_batch([args.keyword_zh], options=options, max_workers=2)
    report = batch_report.keywords[0].to_dict() if batch_report.keywords else batch_report.to_dict()

    prefix = "[OK]" if batch_report.exit_code == 0 else "[ERROR]"
    print(
        f"{prefix} keyword_zh={args.keyword_zh!r} "
        f"status={report['status']} mode={report.get('mode', args.mode)}"
    )
    if "refresh" in report:
        refresh = report["refresh"]
        backfill = report["backfill"]
        candidates = report["candidates"]
        print(
            f"[REFRESH] status={refresh['status']} pages={refresh['pages_requested']} "
            f"recovered={refresh['pages_recovered']} items={refresh['items_returned']} "
            f"failures={refresh['provider_failures']}"
        )
        print(
            f"[BACKFILL] status={backfill['status']} pages={backfill['pages_requested']} "
            f"recovered={backfill['pages_recovered']} committed={backfill['pages_committed']} "
            f"exhausted={backfill['states_exhausted']} failures={backfill['provider_failures']}"
        )
        print(
            f"[CANDIDATES] staged={candidates.get('staged', 0)} "
            f"emitted={candidates.get('emitted', 0)} "
            f"existing={candidates.get('existing_duplicates', 0)} "
            f"duplicate_observation={candidates.get('duplicate_observations', 0)}"
        )

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(batch_report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return batch_report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
