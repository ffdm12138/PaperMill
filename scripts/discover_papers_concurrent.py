"""Run multiple discover_papers.py queries concurrently.

Wrapper that fans out independent keyword queries to discover_papers.py as
subprocesses, one per (deduplicated) keyword, using a thread pool.  Each
query gets its own output directory, report file (in staging mode), and
log file, named with index + batch_stamp to prevent collisions.

Each subprocess runs the dual-lane (Refresh + Backfill) discovery for its
keyword and persists progress to a per-keyword notebook under
``data/discovery/keyword_notebooks/``. ``--max-workers`` bounds how many
keyword subprocesses run at once; provider rate limiting is enforced
inside each subprocess.

Usage:
    conda run -n mineru python scripts/discover_papers_concurrent.py \
        --query "query 1" --query "query 2" --max-workers 4 \
        --mode hybrid --refresh-pages 2 --backfill-pages 5 \
        --stage-to-paper-raw --apply
"""
import argparse
import json
import re
import subprocess
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
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
from src.discovery.coordinator import DiscoveryOptions, run_discovery_batch  # noqa: E402


DISCOVER_SCRIPT = PROJECT_ROOT / "scripts" / "discover_papers.py"


def _slugify(text: str, max_len: int = 60) -> str:
    """Lowercase, collapse non-alnum runs to underscore, truncate."""
    s = re.sub(r"[^\w]", "_", text.lower())
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:max_len].rstrip("_")


def _normalize_keyword(keyword: str) -> str:
    """NFC + whitespace-fold + casefold for uniqueness comparison."""
    if not keyword:
        return ""
    value = unicodedata.normalize("NFC", keyword.strip())
    return re.sub(r"\s+", " ", value).casefold().strip()


def _dedupe_keywords(queries: list[str]) -> list[str]:
    """Drop duplicates by normalized identity, preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        nk = _normalize_keyword(q)
        if not nk or nk in seen:
            continue
        seen.add(nk)
        out.append(q)
    return out


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run multiple discover_papers.py queries concurrently (dual-lane).",
    )
    parser.add_argument("--query", "-q", action="append", default=[], dest="queries",
                        help="Search query (repeatable).")
    parser.add_argument("--queries-file", type=Path, default=None,
                        help="File with one query per line (# comments and blank lines ignored).")
    parser.add_argument("--max-workers", type=int, default=4,
                        help="Maximum concurrent keyword subprocesses (default: 4).")
    parser.add_argument("--max-candidates", type=int, default=50,
                        help="Max NEW candidates per keyword after filtering existing DOIs (default: 50).")
    parser.add_argument("--limit-per-query", type=int, default=None,
                        help="Legacy alias for --page-size.")
    parser.add_argument("--page-size", type=int, default=50,
                        help="Results per provider page (default: 50).")
    parser.add_argument("--mode", choices=["hybrid", "refresh", "backfill"], default="hybrid",
                        help="Discovery lane(s) (default: hybrid).")
    parser.add_argument("--refresh-pages", type=int, default=2,
                        help="Refresh pages from page 1 (default: 2).")
    parser.add_argument("--backfill-pages", type=int, default=5,
                        help="Backfill pages from saved cursor (default: 5).")
    parser.add_argument("--sort", default=None, help="Provider sort param.")
    parser.add_argument("--keyword-notebook-dir", type=Path, default=DISCOVERY_KEYWORD_NOTEBOOK_DIR,
                        help="Directory for per-keyword progress notebooks.")
    parser.add_argument("--pending-pages-dir", type=Path, default=DISCOVERY_PENDING_PAGES_DIR)
    parser.add_argument("--discovery-locks-dir", type=Path, default=DISCOVERY_LOCKS_DIR)
    parser.add_argument("--exports-dir", type=Path, default=DISCOVERY_EXPORTS_DIR)
    parser.add_argument("--until-exhausted", action="store_true",
                        help="Backfill until all states exhausted (bounded by --max-pages-total).")
    parser.add_argument("--max-pages-total", type=int, default=None,
                        help="Global cap on actual provider page network requests.")
    parser.add_argument("--max-pending-candidates", type=int, default=1000)
    parser.add_argument("--resume-pending-candidates", type=int, default=700)
    parser.add_argument("--doi-resolution-budget", type=int, default=10,
                        help="Max Crossref title->DOI lookups per keyword.")
    parser.add_argument("--reset-keyword-progress", action="store_true",
                        help="Reset backfill cursors for all given keywords before running.")
    parser.add_argument("--output-dir", type=Path, default=DISCOVERY_DIR / "doi_candidates",
                        help="Base output directory for DOI candidates.")
    parser.add_argument("--stage-to-paper-raw", action="store_true",
                        help="Pass --stage-to-paper-raw to each subprocess.")
    parser.add_argument("--apply", action="store_true",
                        help="Pass --apply (requires --stage-to-paper-raw).")
    parser.add_argument("--skip-duplicates", action="store_true",
                        help="Pass --skip-duplicates (requires --stage-to-paper-raw).")
    parser.add_argument("--hide-existing", action="store_true",
                        help="Pass --hide-existing to each subprocess (query-phase filter).")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--log-dir", type=Path, default=DISCOVERY_DIR / "logs",
                        help="Directory for per-query log files.")
    parser.add_argument("--report-dir", type=Path, default=DISCOVERY_DIR / "reports",
                        help="Directory for per-query staging report files.")
    args = parser.parse_args(argv)

    # --queries-file
    if args.queries_file is not None:
        if not args.queries_file.is_file():
            parser.error(f"--queries-file not found: {args.queries_file}")
        with args.queries_file.open(encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    args.queries.append(stripped)

    if not args.queries:
        parser.error("at least one --query or a --queries-file is required")
    if args.max_workers < 1:
        parser.error("--max-workers must be >= 1")
    if args.max_candidates < 1:
        parser.error("--max-candidates must be > 0")
    if args.page_size < 1:
        parser.error("--page-size must be >= 1")
    if args.limit_per_query is not None and args.limit_per_query < 1:
        parser.error("--limit-per-query must be >= 1")
    if args.refresh_pages < 1:
        parser.error("--refresh-pages must be >= 1")
    if args.backfill_pages < 1:
        parser.error("--backfill-pages must be >= 1")
    if args.apply and not args.stage_to_paper_raw:
        parser.error("--apply requires --stage-to-paper-raw")
    if args.skip_duplicates and not args.stage_to_paper_raw:
        parser.error("--skip-duplicates requires --stage-to-paper-raw")
    if args.until_exhausted and args.mode not in ("backfill", "hybrid"):
        parser.error("--until-exhausted requires --mode backfill or hybrid")
    if args.until_exhausted and args.max_pages_total is None:
        parser.error("--until-exhausted requires explicit --max-pages-total")
    return args


def _build_command(
    query: str,
    index: int,
    batch_stamp: str,
    args: argparse.Namespace,
) -> tuple[list[str], Path, Path | None, Path]:
    slug = _slugify(query)
    per_query_output = args.output_dir / f"concurrent_{batch_stamp}" / f"{index:03d}_{slug}"

    page_size = args.page_size
    if args.limit_per_query is not None:
        page_size = args.limit_per_query

    cmd = [
        sys.executable,
        str(DISCOVER_SCRIPT),
        query,
        "--output-dir", str(per_query_output),
        "--max-candidates", str(args.max_candidates),
        "--page-size", str(page_size),
        "--mode", args.mode,
        "--refresh-pages", str(args.refresh_pages),
        "--backfill-pages", str(args.backfill_pages),
        "--keyword-notebook-dir", str(args.keyword_notebook_dir),
        "--doi-resolution-budget", str(args.doi_resolution_budget),
    ]
    if args.sort:
        cmd.extend(["--sort", args.sort])
    if args.until_exhausted:
        cmd.append("--until-exhausted")
    if args.max_pages_total is not None:
        cmd.extend(["--max-pages-total", str(args.max_pages_total)])
    if args.reset_keyword_progress:
        cmd.append("--reset-keyword-progress")

    report_path: Path | None = None
    if args.stage_to_paper_raw:
        cmd.append("--stage-to-paper-raw")
        report_path = args.report_dir / f"stage_{index:03d}_{slug}_{batch_stamp}.json"
        cmd.extend(["--report", str(report_path)])
    if args.apply:
        cmd.append("--apply")
    if args.skip_duplicates:
        cmd.append("--skip-duplicates")
    if args.hide_existing:
        cmd.append("--hide-existing")

    log_path = args.log_dir / f"discover_{index:03d}_{slug}_{batch_stamp}.log"
    return cmd, per_query_output, report_path, log_path


def _run_one(
    query: str,
    index: int,
    batch_stamp: str,
    args: argparse.Namespace,
) -> dict:
    """Run one discover_papers.py subprocess and return result metadata."""
    cmd, per_query_output, report_path, log_path = _build_command(query, index, batch_stamp, args)
    started_at = datetime.now(timezone.utc).isoformat()

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log_fh:
        log_fh.write(f"# command: {' '.join(cmd)}\n# started: {started_at}\n")
        log_fh.flush()
        result = subprocess.run(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            cwd=PROJECT_ROOT,
        )
    ended_at = datetime.now(timezone.utc).isoformat()

    return {
        "index": index,
        "query": query,
        "slug": _slugify(query),
        "returncode": result.returncode,
        "output_dir": str(per_query_output),
        "report_path": str(report_path) if report_path else None,
        "log": str(log_path),
        "started_at": started_at,
        "ended_at": ended_at,
    }


def _aggregate_lane_reports(results: list[dict]) -> dict:
    """Aggregate refresh/backfill/candidates/stage from per-query reports."""
    agg = {
        "keywords": {"total": len(results), "full_success": 0, "partial": 0, "failed": 0, "skipped": 0},
        "refresh": {"pages": 0, "items": 0, "provider_failures": 0},
        "backfill": {"pages_advanced": 0, "exhausted_states": 0, "provider_failures": 0},
        "candidates": {"combined": 0, "unique_doi": 0, "existing": 0, "new": 0, "selected": 0},
        "stage": {"staged": 0, "duplicate": 0, "failed": 0, "planned": 0},
    }
    for r in results:
        rp = r.get("report_path")
        if not rp:
            continue
        rp_path = Path(rp)
        if not rp_path.is_file():
            continue
        try:
            data = json.loads(rp_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        status = data.get("status", "failed")
        if status == "success":
            agg["keywords"]["full_success"] += 1
        elif status == "partial_success":
            agg["keywords"]["partial"] += 1
        elif status in ("skipped", "exhausted"):
            agg["keywords"]["skipped"] += 1
        else:
            agg["keywords"]["failed"] += 1
        for key in ("refresh", "backfill", "candidates"):
            section = data.get(key) or {}
            for k, v in section.items():
                if k in agg[key] and isinstance(v, (int, float)):
                    agg[key][k] += v
        stage = data.get("stage") or {}
        for k in ("staged", "duplicate", "failed", "planned"):
            if k in stage:
                agg["stage"][k] += stage[k]
    return agg


def main_internal(argv: list[str]) -> int:
    args = _parse_args(argv)
    batch_stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    started_at = datetime.now(timezone.utc).isoformat()

    args.log_dir.mkdir(parents=True, exist_ok=True)
    args.report_dir.mkdir(parents=True, exist_ok=True)

    # Deduplicate keywords by normalized identity so the same keyword is
    # never run by two subprocesses concurrently (would race on the
    # shared backfill cursor).
    queries = _dedupe_keywords(args.queries)
    duplicates_dropped = len(args.queries) - len(queries)
    n = len(queries)

    page_size = args.page_size
    if args.limit_per_query is not None:
        page_size = args.limit_per_query
    options = DiscoveryOptions(
        mode=args.mode,
        refresh_pages=args.refresh_pages,
        backfill_pages=args.backfill_pages,
        page_size=page_size,
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
    batch = run_discovery_batch(queries, options=options, max_workers=args.max_workers)
    ended_at = datetime.now(timezone.utc).isoformat()
    exit_code = batch.exit_code
    ordered_results = [
        {
            "index": idx,
            "query": report.keyword,
            "returncode": 0 if report.status in {"success", "skipped", "exhausted"} else (2 if report.status == "partial_success" else 1),
            "status": report.status,
        }
        for idx, report in enumerate(batch.keywords)
    ]

    summary: dict = {
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
        "queries_count": n,
        "duplicates_dropped": duplicates_dropped,
        "queries": ordered_results,
        "note": "in-process coordinator; max_workers is the global lane cap",
        "batch_report": batch.to_dict(),
    }
    summary["lane_aggregation"] = batch.aggregate
    summary["stage_summary"] = dict(batch.aggregate.get("candidates", {}))

    report_path = args.report_dir / f"concurrent_discovery_{batch_stamp}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # Print summary (§十六 format)
    if summary.get("lane_aggregation"):
        la = summary["lane_aggregation"]
        kw = la["keywords"]
        print(f"[KEYWORDS] total={kw['total']} success={kw.get('success', 0)} "
              f"partial={kw.get('partial_success', 0)} failed={kw.get('failed', 0)} skipped={kw.get('skipped', 0)}")
        rf = la["refresh"]
        print(f"[REFRESH] pages={rf['pages_requested']} recovered={rf['pages_recovered']} provider_failures={rf['provider_failures']}")
        bf = la["backfill"]
        print(f"[BACKFILL] pages={bf['pages_requested']} recovered={bf['pages_recovered']} "
              f"exhausted_states={bf.get('states_exhausted', bf.get('exhausted_states', 0))} failures={bf['provider_failures']}")
        cd = la["candidates"]
        print(f"[CANDIDATES] staged={cd['staged']} emitted={cd['emitted']} "
              f"existing={cd['existing_duplicates']} duplicate_observation={cd['duplicate_observations']}")
    else:
        failures = [r for r in ordered_results if r["returncode"] != 0]
        ok_count = n - len(failures)
        fail_count = len(failures)
        print(f"[OK] {ok_count}/{n} keywords succeeded, {fail_count} failed")
    failures = [r for r in ordered_results if r["returncode"] != 0]
    if failures:
        for r in failures:
            print(f"  FAIL  [{r['index']}] {r['query']}  (exit {r['returncode']})")
    print(f"[OK] Report: {report_path}")

    return exit_code


def main() -> int:
    return main_internal(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
