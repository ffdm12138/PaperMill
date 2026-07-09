"""Read-only and management operations for discovery keyword notebooks.

Operations:
  --list                          List all keywords with progress summary.
  --show "keyword"                Show detailed notebook state for one keyword.
  --enable "keyword"              Enable a keyword for discovery.
  --disable "keyword"             Disable a keyword (preserves notebook + history).
  --reset-backfill "keyword"      Reset ONE keyword's backfill cursors.

Reset is scoped to a single keyword: it does NOT delete paper_raw, does
NOT delete DOIs, does NOT affect other keywords, and records an entry in
the notebook's ``reset_history``. Batch reset of all keywords is
intentionally not supported.
"""
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import DISCOVERY_KEYWORD_NOTEBOOK_DIR  # noqa: E402
from src.discovery.keyword_notebook import (  # noqa: E402
    KeywordNotebookStore,
    NotebookCorruptError,
    pagination_signature,
)


def _print_keyword_summary(item: dict) -> None:
    print(f"\n{item['keyword']}")
    print(f"  enabled: {'yes' if item.get('enabled') else 'no'}")
    print(f"  keyword_id: {item.get('keyword_id', '')}")
    print(f"  active_expansions: {item.get('active_expansions', 0)}")
    print(f"  updated_at: {item.get('updated_at', '')}")
    stats = item.get("lifetime_statistics") or {}
    print(f"  lifetime: refresh_runs={stats.get('refresh_runs', 0)} "
          f"backfill_runs={stats.get('backfill_runs', 0)} "
          f"unique_dois={stats.get('unique_dois_seen', 0)} "
          f"new_staged={stats.get('new_dois_staged', 0)}")
    for exp in item.get("expansions", []):
        marker = "" if exp.get("active") else "  (inactive)"
        print(f"  expansion: {exp.get('query', '')!r}{marker}")
        for prov, ps in (exp.get("providers") or {}).items():
            r = ps or {}
            print(f"    {prov}: refresh={r.get('refresh_status')} "
                  f"backfill_cursor={r.get('backfill_cursor')} "
                  f"exhausted={r.get('backfill_exhausted')} "
                  f"pages={r.get('backfill_pages')}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manage discovery keyword notebooks (list/show/enable/disable/reset).",
    )
    parser.add_argument("--keyword-notebook-dir", type=Path,
                        default=DISCOVERY_KEYWORD_NOTEBOOK_DIR)
    parser.add_argument("--list", action="store_true", help="List all keywords.")
    parser.add_argument("--show", metavar="KEYWORD", default=None,
                        help="Show one keyword's notebook state.")
    parser.add_argument("--enable", metavar="KEYWORD", default=None,
                        help="Enable a keyword.")
    parser.add_argument("--disable", metavar="KEYWORD", default=None,
                        help="Disable a keyword.")
    parser.add_argument("--reset-backfill", metavar="KEYWORD", default=None,
                        help="Reset ONE keyword's backfill cursors.")
    parser.add_argument("--reason", default="cli manage_discovery_keywords",
                        help="Reason recorded in reset_history (with --reset-backfill).")
    parser.add_argument("--sort", default=None,
                        help="Pagination sort used by the run (for reset signature).")
    args = parser.parse_args()

    store = KeywordNotebookStore(args.keyword_notebook_dir)

    if args.list:
        items = store.list_keywords()
        if not items:
            print("(no keyword notebooks found)")
            return 0
        for item in items:
            _print_keyword_summary(item)
        return 0

    if args.show:
        try:
            store.require(args.show)
            item = store.show(args.show)
        except (FileNotFoundError, NotebookCorruptError) as exc:
            print(f"[ERROR] no notebook for keyword: {args.show!r}", file=sys.stderr)
            return 1
        print(json.dumps(item, ensure_ascii=False, indent=2))
        return 0

    if args.enable:
        try:
            nb = store.set_enabled(args.enable, True)
        except (FileNotFoundError, NotebookCorruptError) as exc:
            print(f"[ERROR] no notebook for keyword: {args.enable!r}", file=sys.stderr)
            return 1
        print(f"[OK] enabled: {args.enable!r}")
        return 0

    if args.disable:
        try:
            nb = store.set_enabled(args.disable, False)
        except (FileNotFoundError, NotebookCorruptError) as exc:
            print(f"[ERROR] no notebook for keyword: {args.disable!r}", file=sys.stderr)
            return 1
        print(f"[OK] disabled: {args.disable!r}")
        return 0

    if args.reset_backfill:
        pag_sig = pagination_signature(sort=args.sort)
        try:
            nb = store.reset_backfill(
                args.reset_backfill, reason=args.reason, pag_sig=pag_sig,
            )
        except FileNotFoundError:
            print(f"[ERROR] no notebook for keyword: {args.reset_backfill!r}", file=sys.stderr)
            return 1
        except NotebookCorruptError as exc:
            print(f"[ERROR] notebook corrupt: {exc}", file=sys.stderr)
            return 1
        if nb is None:
            print(f"[ERROR] no notebook for keyword: {args.reset_backfill!r}", file=sys.stderr)
            return 1
        print(f"[OK] reset backfill for: {args.reset_backfill!r}")
        print(f"     (paper_raw and existing DOIs are NOT affected)")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
