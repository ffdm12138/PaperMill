"""Run 3--8 Chinese discovery notebooks through one shared coordinator.

Each ``--keyword-zh`` selects one classification notebook.  The coordinator
executes every active Chinese and English query stored in that notebook; query
text never becomes a Catalog identity or directory name.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (  # noqa: E402
    DISCOVERY_DIR,
    DISCOVERY_EXPORTS_DIR,
    DISCOVERY_KEYWORD_NOTEBOOK_DIR,
    DISCOVERY_LOCKS_DIR,
    DISCOVERY_PENDING_PAGES_DIR,
    PAPER_NUMBER_LEDGER_PATH,
    PAPER_RAW_DIR,
    PAPERS_DIR,
)
from src.discovery.cli_plan import (  # noqa: E402
    list_enabled_keyword_zh,
    load_keyword_plan,
)
from src.discovery.coordinator import DiscoveryOptions, run_discovery_batch  # noqa: E402


def _normalize_keyword(keyword: str) -> str:
    if not keyword:
        return ""
    value = unicodedata.normalize("NFC", keyword.strip())
    return re.sub(r"\s+", " ", value).casefold().strip()


def _dedupe_keywords(keywords: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for keyword in keywords:
        identity = _normalize_keyword(keyword)
        if not identity or identity in seen:
            continue
        seen.add(identity)
        result.append(keyword.strip())
    return result


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run 3--8 Chinese classification notebooks with 3--4 shared workers.",
    )
    parser.add_argument(
        "--keyword-zh",
        action="append",
        default=[],
        dest="keywords",
        help="Exact Chinese notebook classification keyword (repeatable).",
    )
    parser.add_argument(
        "--keywords-file",
        type=Path,
        default=None,
        help="UTF-8 file containing one keyword_zh per line.",
    )
    parser.add_argument(
        "--from-enabled-notebooks",
        action="store_true",
        help="Select every enabled schema-v3 notebook from --keyword-notebook-dir.",
    )
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--max-candidates", type=int, default=50)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--mode", choices=["hybrid", "refresh", "backfill"], default="hybrid")
    parser.add_argument("--refresh-pages", type=int, default=2)
    parser.add_argument("--backfill-pages", type=int, default=5)
    parser.add_argument("--sort", default=None)
    parser.add_argument("--keyword-notebook-dir", type=Path, default=DISCOVERY_KEYWORD_NOTEBOOK_DIR)
    parser.add_argument("--pending-pages-dir", type=Path, default=DISCOVERY_PENDING_PAGES_DIR)
    parser.add_argument("--discovery-locks-dir", type=Path, default=DISCOVERY_LOCKS_DIR)
    parser.add_argument("--exports-dir", type=Path, default=DISCOVERY_EXPORTS_DIR)
    parser.add_argument("--until-exhausted", action="store_true")
    parser.add_argument("--max-pages-total", type=int, default=None)
    parser.add_argument("--max-pending-candidates", type=int, default=1000)
    parser.add_argument("--resume-pending-candidates", type=int, default=700)
    parser.add_argument("--doi-resolution-budget", type=int, default=10)
    parser.add_argument("--output-dir", type=Path, default=DISCOVERY_DIR / "doi_candidates")
    parser.add_argument("--stage-to-paper-raw", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--skip-duplicates", action="store_true")
    parser.add_argument("--hide-existing", action="store_true")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--report-dir", type=Path, default=DISCOVERY_DIR / "reports")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate notebooks and print all provider lanes without writes or requests.",
    )
    args = parser.parse_args(argv)

    if args.keywords_file is not None:
        if not args.keywords_file.is_file():
            parser.error(f"--keywords-file not found: {args.keywords_file}")
        for line in args.keywords_file.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if value and not value.startswith("#"):
                args.keywords.append(value)
    if args.from_enabled_notebooks:
        try:
            args.keywords.extend(list_enabled_keyword_zh(args.keyword_notebook_dir))
        except (OSError, RuntimeError, ValueError) as exc:
            parser.error(str(exc))

    args.keywords = _dedupe_keywords(args.keywords)
    if not 3 <= len(args.keywords) <= 8:
        parser.error("broad discovery requires 3--8 unique --keyword-zh selections")
    if not 3 <= args.max_workers <= 4:
        parser.error("--max-workers must be 3 or 4")
    if args.max_candidates < 1:
        parser.error("--max-candidates must be > 0")
    if args.page_size < 1:
        parser.error("--page-size must be >= 1")
    if args.refresh_pages < 1:
        parser.error("--refresh-pages must be >= 1")
    if args.backfill_pages < 1:
        parser.error("--backfill-pages must be >= 1")
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
    return args


def main_internal(argv: list[str]) -> int:
    args = _parse_args(argv)

    try:
        plans = [
            load_keyword_plan(
                keyword,
                args.keyword_notebook_dir,
                mode=args.mode,
                refresh_pages=args.refresh_pages,
                backfill_pages=args.backfill_pages,
                max_workers=args.max_workers,
                max_pages_total=args.max_pages_total,
            )
            for keyword in args.keywords
        ]
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[ERROR] discovery preflight failed: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print("[DRY-RUN] validated read-only broad discovery plan")
        print(json.dumps({"schema_version": "3.0", "keywords": plans}, ensure_ascii=False, indent=2))
        return 0

    batch_stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    started_at = datetime.now(timezone.utc).isoformat()
    args.report_dir.mkdir(parents=True, exist_ok=True)

    options = DiscoveryOptions(
        mode=args.mode,
        refresh_pages=args.refresh_pages,
        backfill_pages=args.backfill_pages,
        page_size=args.page_size,
        max_candidates=args.max_candidates,
        output_dir=args.output_dir / f"concurrent_{batch_stamp}",
        notebook_dir=args.keyword_notebook_dir,
        pending_pages_dir=args.pending_pages_dir,
        locks_dir=args.discovery_locks_dir,
        exports_dir=args.exports_dir,
        stage_to_paper_raw=args.stage_to_paper_raw,
        apply=args.apply,
        skip_duplicates=args.skip_duplicates,
        hide_existing=args.hide_existing,
        paper_raw_dir=args.paper_raw_dir,
        papers_dir=args.papers_dir,
        ledger_path=args.ledger_path,
        until_exhausted=args.until_exhausted,
        max_pages_total=args.max_pages_total,
        doi_resolution_budget=args.doi_resolution_budget,
        max_pending_candidates=args.max_pending_candidates,
        resume_pending_candidates=args.resume_pending_candidates,
        openalex_refresh_sort=args.sort,
        openalex_backfill_sort=args.sort,
        crossref_refresh_sort=args.sort,
        crossref_backfill_sort=args.sort,
    )
    batch = run_discovery_batch(args.keywords, options=options, max_workers=args.max_workers)
    ended_at = datetime.now(timezone.utc).isoformat()
    ordered_results = [
        {
            "index": index,
            "keyword_zh": report.keyword_zh,
            "returncode": (
                0 if report.status in {"success", "skipped", "exhausted"}
                else 2 if report.status == "partial_success"
                else 1
            ),
            "status": report.status,
        }
        for index, report in enumerate(batch.keywords)
    ]
    summary: dict[str, object] = {
        "schema_version": "3.0",
        "tool": "discover_papers_concurrent",
        "batch_stamp": batch_stamp,
        "started_at": started_at,
        "ended_at": ended_at,
        "max_workers": args.max_workers,
        "mode": args.mode,
        "refresh_pages": args.refresh_pages,
        "backfill_pages": args.backfill_pages,
        "page_size": args.page_size,
        "max_candidates": args.max_candidates,
        "keywords_count": len(args.keywords),
        "keywords": ordered_results,
        "lane_aggregation": batch.aggregate,
        "stage_summary": dict(batch.aggregate.get("candidates", {})),
        "batch_report": batch.to_dict(),
    }
    report_path = args.report_dir / f"concurrent_discovery_{batch_stamp}.json"
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    aggregate = batch.aggregate
    keyword_counts = aggregate["keywords"]
    print(
        f"[KEYWORDS] total={keyword_counts['total']} success={keyword_counts.get('success', 0)} "
        f"partial={keyword_counts.get('partial_success', 0)} "
        f"failed={keyword_counts.get('failed', 0)} skipped={keyword_counts.get('skipped', 0)}"
    )
    refresh = aggregate["refresh"]
    backfill = aggregate["backfill"]
    candidates = aggregate["candidates"]
    print(
        f"[REFRESH] pages={refresh['pages_requested']} recovered={refresh['pages_recovered']} "
        f"provider_failures={refresh['provider_failures']}"
    )
    print(
        f"[BACKFILL] pages={backfill['pages_requested']} recovered={backfill['pages_recovered']} "
        f"exhausted_states={backfill.get('states_exhausted', 0)} "
        f"failures={backfill['provider_failures']}"
    )
    print(
        f"[CANDIDATES] staged={candidates['staged']} emitted={candidates['emitted']} "
        f"existing={candidates['existing_duplicates']} "
        f"duplicate_observation={candidates['duplicate_observations']}"
    )
    for result in ordered_results:
        if result["returncode"] != 0:
            print(
                f"[ERROR] [{result['index']}] {result['keyword_zh']} "
                f"(exit {result['returncode']})"
            )
    print(f"[REPORT] {report_path}")
    return batch.exit_code


def main() -> int:
    return main_internal(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
