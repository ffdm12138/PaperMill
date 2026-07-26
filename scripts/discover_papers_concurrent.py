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
    PAPER_NUMBER_LEDGER_PATH,
    PAPER_RAW_DIR,
    PAPERS_DIR,
)
from src.discovery.cli_plan import (  # noqa: E402
    list_enabled_keyword_zh,
    load_keyword_plan,
)
from src.discovery.coordinator import (
    DiscoveryOptions,
    DiscoveryRuntimeDependencies,
    run_discovery_batch_with_dependencies,
)  # noqa: E402
from src.discovery.staging_gateway import MetadataStagingGateway  # noqa: E402
from src.discovery.stores.bundle import DiscoveryStoreBundleV4  # noqa: E402
from src.discovery.workspace import DiscoveryWorkspace, WorkspaceResolver  # noqa: E402
from src.discovery.maintenance_gate import (  # noqa: E402
    MigrationMaintenanceLockError,
    assert_discovery_write_allowed,
)


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
        help="Select every enabled v4 notebook from the active workspace.",
    )
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--max-candidates", type=int, default=50)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--mode", choices=["hybrid", "refresh", "backfill"], default="hybrid")
    parser.add_argument("--refresh-pages", type=int, default=2)
    parser.add_argument("--backfill-pages", type=int, default=5)
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=None,
        help="Override the active discovery workspace root (for tests/staging).",
    )
    parser.add_argument("--until-exhausted", action="store_true")
    parser.add_argument("--max-pages-total", type=int, default=None)
    parser.add_argument(
        "--max-provider-requests-total", type=int, default=None,
        help="Batch-wide valve on real HTTP attempts (incl. retries/failures). "
             "In --until-exhausted mode at least one of --max-pages-total / "
             "--max-provider-requests-total is required (no giant-integer runs).",
    )
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

    args.keywords = _dedupe_keywords(args.keywords)
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
    if args.until_exhausted and args.max_pages_total is None and args.max_provider_requests_total is None:
        parser.error(
            "--until-exhausted requires at least one safety valve "
            "(--max-pages-total or --max-provider-requests-total)"
        )
    return args


def _resolve_active_workspace() -> DiscoveryWorkspace | None:
    """Auto-resolve the active v4 discovery workspace.

    Returns ``None`` if no active workspace exists or the resolution fails.
    Production callers must fail closed rather than falling back to legacy flat
    directories.
    """
    try:
        return WorkspaceResolver().resolve_active()
    except Exception:
        return None


def _workspace_from_path(root: Path) -> DiscoveryWorkspace:
    """Build a v4 workspace reference from an explicit root path (test/staging)."""
    root = root.resolve()
    ws = DiscoveryWorkspace(
        generation_id=root.name,
        root=root,
        keyword_notebook_dir=root / "keyword_notebooks",
        lane_states_dir=root / "lane_states",
        page_journals_dir=root / "page_journals",
        indexes_dir=root / "indexes",
        exports_dir=root / "exports",
        reports_dir=root / "reports",
        locks_dir=root / "locks",
    )
    ws.ensure_dirs()
    return ws


def main_internal(argv: list[str]) -> int:
    args = _parse_args(argv)

    # Fail closed while a discovery migration maintenance window is active.
    # The gate is unconditional: --workspace-root (test/staging path) does
    # NOT exempt a run.  The in-process migration smoke passes because it
    # owns the lock in this process; no external CLI can forge that.
    try:
        assert_discovery_write_allowed()
    except MigrationMaintenanceLockError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    # Resolve workspace — production uses the active v4 pointer; tests may
    # override with an explicit workspace root.  No silent fallback to legacy
    # flat directories.
    if args.workspace_root:
        ws = _workspace_from_path(args.workspace_root)
    else:
        ws = _resolve_active_workspace()
        if ws is None:
            print(
                "[ERROR] no active discovery workspace; initialize one or pass "
                "--workspace-root",
                file=sys.stderr,
            )
            return 1

    if args.from_enabled_notebooks:
        try:
            args.keywords.extend(list_enabled_keyword_zh(ws.keyword_notebook_dir))
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"[ERROR] failed to list enabled notebooks: {exc}", file=sys.stderr)
            return 1
        args.keywords = _dedupe_keywords(args.keywords)

    if not 3 <= len(args.keywords) <= 8:
        print(
            "[ERROR] broad discovery requires 3--8 unique keywords; "
            f"got {len(args.keywords)}",
            file=sys.stderr,
        )
        return 1

    try:
        plans = [
            load_keyword_plan(
                keyword,
                ws.keyword_notebook_dir,
                mode=args.mode,
                refresh_pages=args.refresh_pages,
                backfill_pages=args.backfill_pages,
                max_workers=args.max_workers,
                max_pages_total=args.max_pages_total,
                max_provider_requests_total=args.max_provider_requests_total,
            )
            for keyword in args.keywords
        ]
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[ERROR] discovery preflight failed: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print("[DRY-RUN] validated read-only broad discovery plan")
        print(json.dumps({"schema_version": "4.0", "keywords": plans}, ensure_ascii=False, indent=2))
        return 0

    batch_stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    started_at = datetime.now(timezone.utc).isoformat()
    args.report_dir.mkdir(parents=True, exist_ok=True)

    options = DiscoveryOptions(
        workspace=ws,
        mode=args.mode,
        refresh_pages=args.refresh_pages,
        backfill_pages=args.backfill_pages,
        page_size=args.page_size,
        max_candidates=args.max_candidates,
        output_dir=args.output_dir / f"concurrent_{batch_stamp}",
        stage_to_paper_raw=args.stage_to_paper_raw,
        apply=args.apply,
        skip_duplicates=args.skip_duplicates,
        hide_existing=args.hide_existing,
        paper_raw_dir=args.paper_raw_dir,
        papers_dir=args.papers_dir,
        ledger_path=args.ledger_path,
        until_exhausted=args.until_exhausted,
        max_pages_total=args.max_pages_total,
        max_provider_requests_total=args.max_provider_requests_total,
        doi_resolution_budget=args.doi_resolution_budget,
        max_pending_candidates=args.max_pending_candidates,
        resume_pending_candidates=args.resume_pending_candidates,
        title_resolution_cache_dir=DISCOVERY_DIR / "title_resolution_cache",
    )
    deps = DiscoveryRuntimeDependencies(
        bundle=DiscoveryStoreBundleV4.from_workspace(ws),
        paper_raw_dir=args.paper_raw_dir,
        papers_dir=args.papers_dir,
        ledger_path=args.ledger_path,
        locks_dir=ws.locks_dir,
        exports_dir=ws.exports_dir,
        output_dir=ws.exports_dir,
        relevance_cache_dir=DISCOVERY_DIR / "relevance_raw_work_cache",
        title_resolution_cache_dir=DISCOVERY_DIR / "title_resolution_cache",
        metadata_gateway=MetadataStagingGateway(
            paper_raw_dir=args.paper_raw_dir,
            papers_dir=args.papers_dir,
            ledger_path=args.ledger_path,
        ),
    )
    batch = run_discovery_batch_with_dependencies(
        args.keywords, deps=deps, options=options, max_workers=args.max_workers
    )
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
        "schema_version": "4.0",
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
