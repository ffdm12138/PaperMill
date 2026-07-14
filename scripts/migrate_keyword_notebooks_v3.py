#!/usr/bin/env python
"""Plan, apply, resume, or roll back the explicit notebook-v3 migration.

Dry-run is the default and is strictly read-only.  Apply requires a curated
query manifest plus a byte-bound mapping manifest; the migration service never
guesses a Chinese topic from filenames, directories, or search text.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (  # noqa: E402
    DISCOVERY_KEYWORD_NOTEBOOK_DIR,
    DISCOVERY_LOCKS_DIR,
    DISCOVERY_PENDING_PAGES_DIR,
    TRANSACTION_ROOT,
)
from src.discovery.notebook_v3_migration import (  # noqa: E402
    NotebookV3MigrationError,
    inventory_notebooks,
    migrate_notebooks_v3,
    rollback_migration,
)


DEFAULT_QUERY_MANIFEST = PROJECT_ROOT / "config" / "discovery_keyword_queries.json"
DEFAULT_RETIRED_DIR = DISCOVERY_KEYWORD_NOTEBOOK_DIR.parent / "keyword_notebooks_retired"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transactional migration of legacy discovery notebooks to strict schema v3.",
    )
    parser.add_argument("--notebook-dir", type=Path, default=DISCOVERY_KEYWORD_NOTEBOOK_DIR)
    parser.add_argument("--retired-dir", type=Path, default=DEFAULT_RETIRED_DIR)
    parser.add_argument("--pending-pages-dir", type=Path, default=DISCOVERY_PENDING_PAGES_DIR)
    parser.add_argument("--locks-dir", type=Path, default=DISCOVERY_LOCKS_DIR)
    parser.add_argument("--transaction-root", type=Path, default=TRANSACTION_ROOT)
    parser.add_argument("--query-manifest", type=Path, default=DEFAULT_QUERY_MANIFEST)
    parser.add_argument(
        "--mapping-manifest",
        type=Path,
        required=False,
        help="Reviewed source_notebook+sha256 to keyword_zh mapping; never inferred at apply time.",
    )
    parser.add_argument("--transaction-id", default=None)
    parser.add_argument("--write-plan", action="store_true")
    parser.add_argument("--expected-plan-sha256", default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--inventory", action="store_true")
    parser.add_argument("--mapping-template", type=Path, default=Path("mapping.template.json"))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.inventory:
        report = inventory_notebooks(args.notebook_dir)
        template = {"schema_version": "1.0", "mappings": [
            {"source_notebook": row.get("source_notebook"),
             "source_sha256": row.get("source_sha256"),
             "keyword_zh": row.get("suggested_keyword_zh"), "status": "suggested"}
            for row in report["notebooks"] if row.get("source_notebook")
        ]}
        args.mapping_template.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({**report, "mapping_template": str(args.mapping_template)}, ensure_ascii=False, indent=2))
        return 0
    if args.mapping_manifest is None:
        parser.error("--mapping-manifest is required unless --inventory is used")
    if args.resume and (not args.apply or not args.transaction_id):
        parser.error("--resume requires --apply and --transaction-id")
    if args.rollback and (not args.apply or not args.transaction_id):
        parser.error("--rollback requires --apply and --transaction-id")
    if args.resume and args.rollback:
        parser.error("--resume and --rollback are mutually exclusive")
    if args.write_plan and (args.apply or args.resume or args.rollback):
        parser.error("--write-plan cannot be combined with apply/resume/rollback")
    if args.apply and not args.rollback and (not args.transaction_id or not args.expected_plan_sha256):
        parser.error("--apply requires --transaction-id and --expected-plan-sha256")

    common = {
        "notebook_dir": args.notebook_dir,
        "retired_dir": args.retired_dir,
        "pending_pages_dir": args.pending_pages_dir,
        "locks_dir": args.locks_dir,
        "transaction_root": args.transaction_root,
        "query_manifest_path": args.query_manifest,
        "mapping_manifest_path": args.mapping_manifest,
    }
    try:
        if args.rollback:
            result = rollback_migration(**common, tx_id=args.transaction_id)
        else:
            result = migrate_notebooks_v3(
                **common,
                apply=args.apply,
                tx_id=args.transaction_id,
                resume=args.resume,
                write_plan=args.write_plan,
                expected_plan_sha256=args.expected_plan_sha256,
            )
    except (NotebookV3MigrationError, OSError, ValueError) as exc:
        print(f"[BLOCKED] {type(exc).__name__}: {exc}", file=sys.stderr)
        findings = getattr(exc, "findings", None)
        if findings:
            print(json.dumps(findings, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    label = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[{label}] strict notebook-v3 migration")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
