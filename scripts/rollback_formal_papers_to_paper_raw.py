"""CLI for rolling back formal papers to numeric paper_raw.

Supports ``--paper-number``, ``--paper-name``, or ``--all-papers`` as
mutually exclusive target selectors.  Default mode is dry-run; pass
``--apply`` to execute.  Use ``--report PATH`` for structured JSON output.

Core transaction logic lives in :mod:`src.ingest.rollback`.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from config.settings import CATALOG_FOLDER_ROOT, PAPER_NUMBER_LEDGER_PATH, PAPER_RAW_DIR, PAPERS_DIR
from src.ingest.rollback import (
    discover_all_papers_rollback_targets,
    resolve_paper_number_by_paper_name,
    rollback_formal_papers,
)
from src.library.paper_number_ledger import PaperNumberLedger
from src.utils.identifiers import PAPER_NUMBER_RE


def _resolve_paper_number(
    *,
    paper_name: str,
    papers_dir: Path,
    paper_raw_root: Path,
    transaction_root: Path,
    ledger: PaperNumberLedger,
) -> str:
    """Resolve paper_name → paper_number, raising on failure."""
    return resolve_paper_number_by_paper_name(
        paper_name=paper_name,
        papers_dir=papers_dir,
        paper_raw_root=paper_raw_root,
        transaction_root=transaction_root,
        ledger=ledger,
    )

EXIT_OK = 0
EXIT_ROLLBACK_FAILED = 1
EXIT_CLI_ERROR = 2
EXIT_BLOCKING = 3

PaperStatus = Literal[
    "planned", "resumed", "completed", "failed", "not_started", "blocked"
]

REPORT_SCHEMA_VERSION = "1.0"


@dataclass
class PaperResult:
    paper_number: str = ""
    paper_name: str = ""
    status: PaperStatus = "planned"
    transaction_id: str | None = None
    reason_code: str | None = None
    message: str | None = None


@dataclass
class RollbackReport:
    schema_version: str = REPORT_SCHEMA_VERSION
    operation: str = "rollback_formal_to_paper_raw"
    mode: Literal["dry_run", "apply"] = "dry_run"
    requested_target: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, int] = field(default_factory=lambda: {
        "discovered": 0,
        "planned": 0,
        "completed": 0,
        "failed": 0,
        "not_started": 0,
        "blocking_errors": 0,
    })
    papers: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operation": self.operation,
            "mode": self.mode,
            "requested_target": self.requested_target,
            "summary": dict(self.summary),
            "papers": list(self.papers),
        }

    def add_paper_result(self, result: PaperResult) -> None:
        self.papers.append({
            "paper_number": result.paper_number,
            "paper_name": result.paper_name,
            "status": result.status,
            "transaction_id": result.transaction_id,
            "reason_code": result.reason_code,
            "message": result.message,
        })


def _write_report(report_path: str, report: RollbackReport) -> None:
    """Atomically write the rollback report as JSON."""
    from src.utils.atomic_io import atomic_write_json

    rp = Path(report_path)
    if rp.suffix.lower() != ".json":
        raise ValueError("report path must have a .json extension")
    rp.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(rp, report.to_dict(), indent=2)


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments and enforce the required target selector."""
    parser = argparse.ArgumentParser(
        description="Transactionally roll back formal papers to paper_raw"
    )
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument(
        "--paper-number",
        help="16-digit paper number to roll back",
    )
    target_group.add_argument(
        "--paper-name",
        help="Paper name to roll back",
    )
    target_group.add_argument(
        "--all-papers",
        action="store_true",
        help="Roll back ALL formal papers",
    )
    parser.add_argument(
        "--papers-dir",
        default=str(PAPERS_DIR),
        help="Formal papers directory",
    )
    parser.add_argument(
        "--paper-raw-root",
        default=str(PAPER_RAW_DIR),
        help="Paper raw directory",
    )
    parser.add_argument(
        "--transaction-root",
        default=str(PAPER_RAW_DIR.parent / "transactions"),
        help="Transaction journal root",
    )
    parser.add_argument(
        "--ledger-path",
        default=str(PAPER_NUMBER_LEDGER_PATH),
        help="Ledger path",
    )
    parser.add_argument(
        "--catalog-root",
        default=str(CATALOG_FOLDER_ROOT),
        help="Catalog folder root",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute rollback (default is dry-run)",
    )
    parser.add_argument(
        "--report",
        type=str,
        default="",
        metavar="PATH",
        help="Write structured JSON report to PATH",
    )
    args = parser.parse_args()

    if not (args.paper_number or args.paper_name or args.all_papers):
        parser.error("one of --paper-number, --paper-name, or --all-papers is required")
    return args


def _collect_targets(
    args: argparse.Namespace,
    mode: Literal["dry_run", "apply"],
    report: RollbackReport,
    *,
    papers_dir: Path,
    paper_raw_root: Path,
    transaction_root: Path,
    ledger: PaperNumberLedger,
) -> tuple[int | None, list[dict[str, str | None]]]:
    """Build the rollback target list; returns (early_exit_code, targets)."""
    if args.all_papers:
        targets, blocking = discover_all_papers_rollback_targets(papers_dir, ledger, paper_raw_root=paper_raw_root, transaction_root=transaction_root)
        report.summary["discovered"] = len(targets) + len(blocking)
        report.summary["planned"] = len(targets)
        report.summary["blocking_errors"] = len(blocking)

        for b in blocking:
            report.add_paper_result(PaperResult(
                paper_number=b.get("paper_number", ""),
                paper_name=b.get("paper_name", b.get("dir", "")),
                status="blocked",
                reason_code=b.get("error", "unknown"),
                message=b.get("message", ""),
            ))
        for t in targets:
            report.add_paper_result(PaperResult(
                paper_number=t["paper_number"],
                paper_name=t["paper_name"],
                status="planned",
            ))

        if blocking and mode == "apply":
            print(f"[BLOCKED] {len(blocking)} blocking error(s); no rollback executed.", flush=True)
            for b in blocking:
                print(f"  {b.get('paper_number','')} {b.get('paper_name',b.get('dir',''))}: {b.get('error','')}", flush=True)
            if args.report:
                _write_report(args.report, report)
            return EXIT_BLOCKING, []

        if mode == "dry_run":
            print(f"[DRY-RUN] Discovered {len(targets)} formal paper(s) to roll back.", flush=True)
            if blocking:
                print(f"[DRY-RUN] {len(blocking)} blocking error(s) — cannot apply:", flush=True)
                for b in blocking:
                    print(f"  {b.get('paper_number','')} {b.get('paper_name',b.get('dir',''))}: {b.get('error','')}", flush=True)
            else:
                for t in targets:
                    print(f"  Would roll back: number={t['paper_number']}, paper_name={t['paper_name']}", flush=True)
            if args.report:
                _write_report(args.report, report)
            return (EXIT_BLOCKING if blocking else EXIT_OK), []

        # apply mode, no blocking
        print(f"[APPLY] Rolling back {len(targets)} formal paper(s)...", flush=True)

        paper_numbers_or_ids: list[dict[str, str | None]] = [
            {"paper_number": t["paper_number"], "paper_name": None}
            for t in targets
        ]
    elif args.paper_number:
        paper_numbers_or_ids = [{"paper_number": args.paper_number, "paper_name": None}]
    elif args.paper_name:
        paper_numbers_or_ids = [{"paper_number": None, "paper_name": args.paper_name}]
    else:
        assert False, "unreachable"
    return None, paper_numbers_or_ids


def _dry_run_single(
    args: argparse.Namespace,
    report: RollbackReport,
    paper_numbers_or_ids: list[dict[str, str | None]],
    *,
    papers_dir: Path,
    paper_raw_root: Path,
    transaction_root: Path,
    ledger: PaperNumberLedger,
) -> int:
    """Resolve and validate the single-paper dry-run target, printing the plan."""
    # Resolve and validate single-paper target
    for item in paper_numbers_or_ids:
        if item["paper_number"]:
            if not PAPER_NUMBER_RE.match(item["paper_number"]):
                print(f"[ERROR] invalid paper_number: {item['paper_number']}", flush=True)
                if args.report:
                    report.summary["blocking_errors"] = 1
                    report.add_paper_result(PaperResult(
                        paper_number=item["paper_number"],
                        status="blocked",
                        reason_code="invalid_paper_number",
                    ))
                    _write_report(args.report, report)
                return EXIT_CLI_ERROR
            pnum = item["paper_number"]
            pid_display = "(from paper_number)"
        elif item["paper_name"]:
            pid = item["paper_name"]
            if "/" in pid or "\\" in pid or ".." in pid:
                print(f"[ERROR] invalid paper_name (contains path characters): {pid}", flush=True)
                return EXIT_CLI_ERROR
            try:
                pnum = _resolve_paper_number(
                    paper_name=pid,
                    papers_dir=papers_dir,
                    paper_raw_root=paper_raw_root,
                    transaction_root=transaction_root,
                    ledger=ledger,
                )
            except Exception as exc:
                print(f"[ERROR] paper_name not found: {pid} ({exc})", flush=True)
                if args.report:
                    report.summary["blocking_errors"] = 1
                    report.add_paper_result(PaperResult(
                        paper_name=pid,
                        status="blocked",
                        reason_code="paper_name_not_found",
                        message=str(exc),
                    ))
                    _write_report(args.report, report)
                return EXIT_CLI_ERROR
            pid_display = pid
        else:
            assert False, "unreachable"
        print(f"[DRY-RUN] Would roll back: number={pnum}, paper_name={pid_display}", flush=True)
        report.summary["discovered"] = len(paper_numbers_or_ids)
        report.summary["planned"] = len(paper_numbers_or_ids)
        report.add_paper_result(PaperResult(
            paper_number=pnum,
            paper_name=item["paper_name"] or "",
            status="planned",
        ))
    if args.report:
        _write_report(args.report, report)
    return EXIT_OK


def _rollback_one(
    item: dict[str, str | None],
    report: RollbackReport,
    *,
    papers_dir: Path,
    paper_raw_root: Path,
    transaction_root: Path,
    ledger_path: Path,
    catalog_root: Path,
) -> PaperResult:
    """Roll back one target, updating summary counters and printing progress."""
    pnum = item["paper_number"]
    pid = item["paper_name"]
    label = f"number={pnum or '?'} paper_name={pid or '?'}"
    try:
        print(f"  Rolling back: {label}", flush=True)
        actual_number = rollback_formal_papers(
            papers_dir=papers_dir,
            paper_raw_root=paper_raw_root,
            transaction_root=transaction_root,
            ledger_path=ledger_path,
            catalog_root=catalog_root,
            paper_number=pnum,
            paper_name=pid,
        )
        result = PaperResult(
            paper_number=actual_number,
            paper_name=pid or "",
            status="completed",
        )
        report.summary["completed"] += 1
        print(f"    → completed", flush=True)
    except RuntimeError as exc:
        msg = str(exc)
        result = PaperResult(
            paper_number=pnum or "",
            paper_name=pid or "",
            status="failed",
            reason_code="runtime_error",
            message=msg,
        )
        report.summary["failed"] += 1
        print(f"    → FAILED: {msg}", flush=True)
    except Exception as exc:
        msg = str(exc)
        result = PaperResult(
            paper_number=pnum or "",
            paper_name=pid or "",
            status="failed",
            reason_code="error",
            message=msg,
        )
        report.summary["failed"] += 1
        print(f"    → FAILED: {msg}", flush=True)
    return result


def _emit_report(
    args: argparse.Namespace,
    report: RollbackReport,
    results: list[PaperResult],
    paper_numbers_or_ids: list[dict[str, str | None]],
) -> int:
    """Finalize the report (not_started, flush, write) and return the exit code."""
    # Mark remaining as not_started
    done = report.summary["completed"] + report.summary["failed"]
    if done < len(paper_numbers_or_ids):
        for item in paper_numbers_or_ids[done:]:
            results.append(PaperResult(
                paper_number=item["paper_number"] or "",
                paper_name=item["paper_name"] or "",
                status="not_started",
            ))
        report.summary["not_started"] = len(paper_numbers_or_ids) - done

    # Flush paper results into report
    if not args.all_papers:
        for r in results:
            report.add_paper_result(r)

    if args.report:
        _write_report(args.report, report)

    if report.summary["failed"] > 0:
        print(f"\nCompleted: {report.summary['completed']}, "
              f"Failed: {report.summary['failed']}, "
              f"Not started: {report.summary['not_started']}", flush=True)
        return EXIT_ROLLBACK_FAILED

    print(f"\nRolled back {report.summary['completed']} paper(s)", flush=True)
    return EXIT_OK


def main() -> int:
    args = _parse_args()

    mode: Literal["dry_run", "apply"] = "apply" if args.apply else "dry_run"
    report = RollbackReport(
        mode=mode,
        requested_target={
            "paper_number": args.paper_number or None,
            "paper_name": args.paper_name or None,
            "all_papers": args.all_papers,
        },
    )

    papers_dir = Path(args.papers_dir)
    paper_raw_root = Path(args.paper_raw_root)
    transaction_root = Path(args.transaction_root)
    ledger_path = Path(args.ledger_path)
    catalog_root = Path(args.catalog_root)
    ledger = PaperNumberLedger(ledger_path)

    # ── Build target list ──────────────────────────────────────────
    exit_code, paper_numbers_or_ids = _collect_targets(
        args, mode, report,
        papers_dir=papers_dir,
        paper_raw_root=paper_raw_root,
        transaction_root=transaction_root,
        ledger=ledger,
    )
    if exit_code is not None:
        return exit_code
    # ────────────────────────────────────────────────────────────────

    if mode == "dry_run" and not args.all_papers:
        return _dry_run_single(
            args, report, paper_numbers_or_ids,
            papers_dir=papers_dir,
            paper_raw_root=paper_raw_root,
            transaction_root=transaction_root,
            ledger=ledger,
        )

    # ── Apply single or batch ─────────────────────────────────────
    results: list[PaperResult] = []
    report.summary["discovered"] = max(report.summary["discovered"], len(paper_numbers_or_ids))
    report.summary["planned"] = max(report.summary["planned"], len(paper_numbers_or_ids))

    for item in paper_numbers_or_ids:
        result = _rollback_one(
            item, report,
            papers_dir=papers_dir,
            paper_raw_root=paper_raw_root,
            transaction_root=transaction_root,
            ledger_path=ledger_path,
            catalog_root=catalog_root,
        )
        results.append(result)
        if result.status == "failed":
            break  # stop on first failure

    return _emit_report(args, report, results, paper_numbers_or_ids)


if __name__ == "__main__":
    sys.exit(main())
