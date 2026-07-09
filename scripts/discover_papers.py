"""Search scholarly APIs and write DOI candidates for manual review.

Dual-lane discovery (default ``--mode hybrid``): each keyword runs both a
Refresh lane (rescan first pages) and a Backfill lane (resume from the
keyword notebook cursor). Progress is persisted per-keyword under
``data/discovery/keyword_notebooks/`` so that Backfill never restarts
from page 1.
"""
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
from src.discovery.coordinator import DiscoveryOptions, run_discovery_batch  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover DOI candidates from OpenAlex and CrossRef (dual-lane).",
        epilog=(
            "For broad keyword discovery, prefer scripts/discover_papers_concurrent.py "
            "or run several focused queries concurrently (see AGENTS.md §9). "
            "Staging is safe: paper_raw writes are serialized by .paper_raw_write.lock."
        ),
    )
    parser.add_argument("query", help="Chinese or English literature search query.")
    parser.add_argument("--topic", default=None)
    parser.add_argument("--mode", choices=["hybrid", "refresh", "backfill"], default="hybrid",
                        help="Discovery lane(s) to run (default: hybrid).")
    parser.add_argument("--refresh-pages", type=int, default=2,
                        help="Pages to scan from page 1 in the Refresh lane (default: 2).")
    parser.add_argument("--backfill-pages", type=int, default=5,
                        help="Pages to advance from the saved cursor in Backfill (default: 5).")
    parser.add_argument("--page-size", type=int, default=50,
                        help="Results per provider page (default: 50).")
    parser.add_argument("--limit-per-query", type=int, default=None,
                        help="Legacy alias for --page-size (single-shot compat).")
    parser.add_argument("--max-candidates", type=int, default=50,
                        help="Max NEW candidates after filtering existing DOIs (default: 50).")
    parser.add_argument("--output-dir", type=Path, default=DISCOVERY_DIR / "doi_candidates")
    parser.add_argument("--keyword-notebook-dir", type=Path, default=DISCOVERY_KEYWORD_NOTEBOOK_DIR,
                        help="Directory for per-keyword progress notebooks.")
    parser.add_argument("--pending-pages-dir", type=Path, default=DISCOVERY_PENDING_PAGES_DIR)
    parser.add_argument("--discovery-locks-dir", type=Path, default=DISCOVERY_LOCKS_DIR)
    parser.add_argument("--exports-dir", type=Path, default=DISCOVERY_EXPORTS_DIR)
    parser.add_argument("--sort", default=None,
                        help="Compatibility sort override applied to all provider/lane sorts.")
    parser.add_argument("--openalex-refresh-sort", default=None)
    parser.add_argument("--openalex-backfill-sort", default=None)
    parser.add_argument("--crossref-refresh-sort", default=None)
    parser.add_argument("--crossref-backfill-sort", default=None)
    parser.add_argument("--until-exhausted", action="store_true",
                        help="Backfill until all states exhausted (bounded by --max-pages-total).")
    parser.add_argument("--max-pages-total", type=int, default=None,
                        help="Global cap on actual provider page network requests.")
    parser.add_argument("--max-pending-candidates", type=int, default=1000)
    parser.add_argument("--resume-pending-candidates", type=int, default=700)
    parser.add_argument("--doi-resolution-budget", type=int, default=10,
                        help="Max Crossref title->DOI lookups for missing-DOI candidates.")
    parser.add_argument("--reset-keyword-progress", action="store_true",
                        help="Reset this keyword's backfill cursors before running.")
    # direct staging into paper_raw
    parser.add_argument("--stage-to-paper-raw", action="store_true",
                        help="Stage valid-DOI candidates directly into paper_raw workspaces.")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--skip-duplicates", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Allocate paper_raw workspaces (without this, dry-run only).")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--hide-existing", action="store_true",
                        help="Hide candidates whose DOI already exists in paper_raw or papers.")
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    page_size = args.page_size
    if args.limit_per_query is not None:
        page_size = args.limit_per_query
    if page_size < 1:
        parser.error("--page-size must be >= 1")
    if args.refresh_pages < 1:
        parser.error("--refresh-pages must be >= 1")
    if args.backfill_pages < 1:
        parser.error("--backfill-pages must be >= 1")
    if args.max_candidates < 1:
        parser.error("--max-candidates must be >= 1")
    if args.apply and not args.stage_to_paper_raw:
        parser.error("--apply requires --stage-to-paper-raw")
    if args.skip_duplicates and not args.stage_to_paper_raw:
        parser.error("--skip-duplicates requires --stage-to-paper-raw")
    if args.until_exhausted and args.mode not in ("backfill", "hybrid"):
        parser.error("--until-exhausted requires --mode backfill or hybrid")
    if args.until_exhausted and args.max_pages_total is None:
        parser.error("--until-exhausted requires explicit --max-pages-total")

    if args.reset_keyword_progress:
        from src.discovery.keyword_notebook import KeywordNotebookStore, pagination_signature

        store = KeywordNotebookStore(args.keyword_notebook_dir)
        pag_sig = pagination_signature(sort=args.sort)
        store.reset_backfill(args.query, reason="cli --reset-keyword-progress", pag_sig=pag_sig)

    options = DiscoveryOptions(
        mode=args.mode,
        refresh_pages=args.refresh_pages,
        backfill_pages=args.backfill_pages,
        page_size=page_size,
        domain_id=args.topic,
        max_candidates=args.max_candidates,
        output_dir=args.output_dir,
        paper_raw_dir=args.paper_raw_dir,
        papers_dir=args.papers_dir,
        ledger_path=args.ledger_path,
        stage_to_paper_raw=args.stage_to_paper_raw,
        apply=args.apply and not args.dry_run,
        skip_duplicates=args.skip_duplicates,
        hide_existing=args.hide_existing,
        notebook_dir=args.keyword_notebook_dir,
        pending_pages_dir=args.pending_pages_dir,
        locks_dir=args.discovery_locks_dir,
        exports_dir=args.exports_dir,
        openalex_refresh_sort=args.openalex_refresh_sort or args.sort,
        openalex_backfill_sort=args.openalex_backfill_sort or args.sort,
        crossref_refresh_sort=args.crossref_refresh_sort or args.sort,
        crossref_backfill_sort=args.crossref_backfill_sort or args.sort,
        until_exhausted=args.until_exhausted,
        max_pages_total=args.max_pages_total,
        doi_resolution_budget=args.doi_resolution_budget,
        max_pending_candidates=args.max_pending_candidates,
        resume_pending_candidates=args.resume_pending_candidates,
    )
    batch_report = run_discovery_batch([args.query], options=options, max_workers=2)
    report = batch_report.keywords[0].to_dict() if batch_report.keywords else batch_report.to_dict()

    print(f"[OK] keyword={args.query!r} status={report['status']} mode={report.get('mode', args.mode)}")
    r = report["refresh"]
    b = report["backfill"]
    c = report["candidates"]
    print(f"[REFRESH] status={r['status']} pages={r['pages_requested']} recovered={r['pages_recovered']} items={r['items_returned']} failures={r['provider_failures']}")
    print(f"[BACKFILL] status={b['status']} pages={b['pages_requested']} recovered={b['pages_recovered']} committed={b['pages_committed']} exhausted={b['states_exhausted']} failures={b['provider_failures']}")
    print(f"[CANDIDATES] staged={c.get('staged', 0)} emitted={c.get('emitted', 0)} existing={c.get('existing_duplicates', 0)} duplicate_observation={c.get('duplicate_observations', 0)}")
    print(f"[OK] exports/staging processed under: {args.output_dir}")

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(batch_report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    return batch_report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
