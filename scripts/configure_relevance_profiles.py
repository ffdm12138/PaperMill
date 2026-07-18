"""Plan/apply notebook-scoped discovery relevance profiles.

``--plan`` is read-only with respect to notebooks and page journals.  It does
read the complete OpenAlex subfield taxonomy and writes only the requested
JSON report.  ``--apply`` is plan-hash bound and resumes its sole durable
transaction journal after a crash.
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
    DISCOVERY_PENDING_PAGES_DIR,
    DATA_DIR,
)
from src.discovery.relevance_profiles import (  # noqa: E402
    RelevanceProfilePlanError,
    RelevanceProfileTransactionError,
    abort_relevance_profile_transaction,
    apply_relevance_profile_plan,
    build_relevance_profile_plan,
    inspect_relevance_profile_transaction,
    resume_relevance_profile_transaction,
)
from src.discovery.relevance_runtime import RelevanceRuntimePaths  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Configure notebook-scoped relevance profiles.")
    parser.add_argument(
        "--plan", nargs="?", const="__PLAN_MODE__", default=None,
        help="Resolve taxonomy in plan mode, or provide a plan JSON path with --apply.",
    )
    parser.add_argument("--apply", action="store_true", help="Apply a plan-bound transaction.")
    parser.add_argument("--resume", type=Path, metavar="TRANSACTION")
    parser.add_argument("--inspect-transaction", type=Path, metavar="TRANSACTION")
    parser.add_argument("--abort", type=Path, metavar="TRANSACTION")
    parser.add_argument("--profiles", type=Path, help="Profile definitions JSON (required for --plan).")
    parser.add_argument("--json-report", type=Path, help="Plan/report JSON path.")
    parser.add_argument("--plan-file", type=Path, help="Plan JSON path for --apply (alias: --plan-report).")
    parser.add_argument("--plan-report", dest="plan_file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--expected-plan-hash", help="Required SHA-256 plan hash for --apply.")
    parser.add_argument("--taxonomy-snapshot", type=Path, help="Pre-fetched taxonomy snapshot JSON (offline --plan).")
    parser.add_argument("--allow-network-taxonomy", action="store_true",
                        help="Authorize real OpenAlex taxonomy fetch during --plan.")
    parser.add_argument("--notebook-root", "--keyword-notebook-dir", dest="keyword_notebook_dir", type=Path, default=DISCOVERY_KEYWORD_NOTEBOOK_DIR)
    parser.add_argument("--journal-root", "--pending-pages-dir", dest="pending_pages_dir", type=Path, default=DISCOVERY_PENDING_PAGES_DIR)
    parser.add_argument(
        "--transaction-root", "--transactions-root", dest="transactions_root",
        type=Path,
        default=DATA_DIR / "transactions" / "relevance_profiles",
    )
    parser.add_argument("--allow-runtime-write", action="store_true")
    parser.add_argument("--expected-runtime-root", type=Path)
    return parser


def _assert_runtime_write_authorized(roots: list[Path], args: argparse.Namespace) -> None:
    runtime = Path(DATA_DIR).resolve()
    touches_runtime = any(
        root.resolve() == runtime or runtime in root.resolve().parents for root in roots
    )
    if not touches_runtime:
        return
    expected = args.expected_runtime_root.resolve() if args.expected_runtime_root else None
    if not args.allow_runtime_write or expected != runtime:
        raise RelevanceProfileTransactionError(
            "runtime write requires --allow-runtime-write and --expected-runtime-root "
            f"equal to {runtime}"
        )


def _plan_from_transaction(path: Path) -> dict:
    journal = inspect_relevance_profile_transaction(path)
    plan = journal.get("plan")
    if not isinstance(plan, dict):
        raise RelevanceProfileTransactionError("transaction journal has no embedded plan")
    return plan


def _plan_write_roots(plan: dict) -> list[Path]:
    rp = RelevanceRuntimePaths.from_plan(plan)
    return [rp.notebook_root, rp.journal_root, rp.transaction_root]


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        modes = [args.plan is not None and not args.apply, args.apply, args.resume is not None,
                 args.inspect_transaction is not None, args.abort is not None]
        if sum(bool(value) for value in modes) != 1:
            parser.error("exactly one of --plan, --apply, --resume, --inspect-transaction or --abort is required")
        if args.inspect_transaction is not None:
            print(json.dumps(
                inspect_relevance_profile_transaction(args.inspect_transaction),
                ensure_ascii=False, indent=2,
            ))
            return 0
        if args.resume is not None:
            plan = _plan_from_transaction(args.resume)
            _assert_runtime_write_authorized(_plan_write_roots(plan), args)
            result = resume_relevance_profile_transaction(args.resume)
            print(f"[COMMITTED] transaction_id={result['transaction_id']}")
            return 0
        if args.abort is not None:
            plan = _plan_from_transaction(args.abort)
            _assert_runtime_write_authorized(_plan_write_roots(plan), args)
            result = abort_relevance_profile_transaction(args.abort)
            print(f"[ABORTED] transaction_id={result['transaction_id']}")
            return 0
        plan_mode = args.plan == "__PLAN_MODE__" or (args.plan is not None and not args.apply)
        if plan_mode:
            if args.profiles is None or args.json_report is None:
                parser.error("--plan requires --profiles and --json-report")
            snapshot = args.taxonomy_snapshot is not None
            network = args.allow_network_taxonomy
            if snapshot == network:
                parser.error(
                    "--plan requires exactly one of --taxonomy-snapshot FILE "
                    "or --allow-network-taxonomy"
                )
            taxonomy_kwargs = {}
            if snapshot:
                from src.discovery.relevance_profiles import (
                    TaxonomySnapshot, validate_taxonomy_snapshot,
                )
                raw = json.loads(args.taxonomy_snapshot.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise RelevanceProfileTransactionError(
                        "taxonomy snapshot must be a JSON object")
                # Full validation BEFORE constructing the object — closes
                # the bypass where malformed/hash-mismatched snapshots were
                # silently accepted.
                violations = validate_taxonomy_snapshot(raw)
                if violations:
                    raise RelevanceProfileTransactionError(
                        "taxonomy snapshot validation failed: "
                        + "; ".join(violations))
                taxonomy_kwargs["taxonomy"] = TaxonomySnapshot(
                    pages=tuple(raw.get("pages") or ()),
                    entities=tuple(raw.get("entities") or ()),
                    retrieved_at=str(raw.get("retrieved_at") or ""),
                    page_hashes=tuple(raw.get("page_hashes") or ()),
                    snapshot_sha256=str(raw.get("snapshot_sha256") or ""),
                    schema_version=str(raw.get("schema_version") or "1.0"),
                    raw_snapshot_sha256=str(raw.get("raw_snapshot_sha256") or ""),
                    taxonomy_semantic_sha256=str(
                        raw.get("taxonomy_semantic_sha256") or ""),
                )
            runtime_paths = RelevanceRuntimePaths.resolve(
                notebook_root=args.keyword_notebook_dir,
                journal_root=args.pending_pages_dir,
                transaction_root=args.transactions_root,
            )
            plan = build_relevance_profile_plan(
                profiles_path=args.profiles,
                notebook_dir=runtime_paths.notebook_root,
                pending_pages_dir=runtime_paths.journal_root,
                transaction_root=runtime_paths.transaction_root,
                runtime_paths=runtime_paths,
                **taxonomy_kwargs,
            )
            args.json_report.parent.mkdir(parents=True, exist_ok=True)
            args.json_report.write_text(
                json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"[PLAN] {args.json_report}")
            print(f"[PLAN-HASH] {plan['plan_hash']}")
            return 0
        plan_path = (
            Path(args.plan) if args.plan not in (None, "__PLAN_MODE__")
            else args.plan_file or args.json_report
        )
        if plan_path is None or not args.expected_plan_hash:
            parser.error("--apply requires --plan/--plan-report and --expected-plan-hash")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if not isinstance(plan, dict):
            raise RelevanceProfileTransactionError("plan JSON must be an object")
        _assert_runtime_write_authorized(_plan_write_roots(plan), args)
        result = apply_relevance_profile_plan(plan, expected_plan_hash=args.expected_plan_hash)
        print(f"[COMMITTED] transaction_id={result['transaction_id']}")
        return 0
    except RelevanceProfilePlanError as exc:
        if args.json_report is not None:
            args.json_report.parent.mkdir(parents=True, exist_ok=True)
            args.json_report.write_text(
                json.dumps(exc.report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        print(f"[ERROR] relevance profile plan failed: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, RelevanceProfileTransactionError) as exc:
        print(f"[ERROR] relevance profile configuration failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
