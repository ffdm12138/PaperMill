"""CLI for rolling back formal papers to numeric paper_raw.

Supports ``--paper-number``, ``--paper-id``, or ``--all-papers`` as
mutually exclusive target selectors.  Default mode is dry-run; pass
``--apply`` to execute.  Use ``--report PATH`` for structured JSON output.

Core transaction logic lives in :mod:`src.ingest.rollback`.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import CATALOG_FOLDER_ROOT, PAPER_NUMBER_LEDGER_PATH, PAPER_RAW_DIR, PAPERS_DIR
from src.ingest.rollback import resolve_paper_number_by_paper_id, rollback_formal_papers
from src.ingest.transactions import find_active_transaction_for_paper
from src.library.paper_number_ledger import PaperNumberLedger
from src.library.validation import validate_formal_paper
from src.services.ingest_ids import PAPER_NUMBER_RE


def _resolve_paper_number(
    *,
    paper_id: str,
    papers_dir: Path,
    paper_raw_root: Path,
    transaction_root: Path,
    ledger: PaperNumberLedger,
) -> str:
    """Resolve paper_id → paper_number, raising on failure."""
    return resolve_paper_number_by_paper_id(
        paper_id=paper_id,
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
    paper_id: str = ""
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
            "paper_id": result.paper_id,
            "status": result.status,
            "transaction_id": result.transaction_id,
            "reason_code": result.reason_code,
            "message": result.message,
        })


def _build_string_path(path: str) -> str:
    """Ensure a path argument string is suitable for writing."""
    # Prevent empty strings
    if not path.strip():
        return path
    return path


def _discover_all_papers_targets(
    papers_dir: Path,
    ledger: PaperNumberLedger,
    *, paper_raw_root: Path, transaction_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Discover all valid formal papers, returning (valid_targets, blocking_errors)."""
    targets: list[dict[str, Any]] = []
    blocking: list[dict[str, Any]] = []

    if not papers_dir.is_dir():
        return targets, blocking

    # Scan .paper.number markers first
    marker_map: dict[str, str] = {}  # paper_number -> dir_name
    for candidate in sorted(papers_dir.iterdir()):
        if not candidate.is_dir() or candidate.name.startswith("."):
            continue
        markers = list(candidate.glob("*.paper.number"))
        if markers:
            try:
                data = json.loads(markers[0].read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                blocking.append({
                    "dir": candidate.name,
                    "error": "corrupt_marker",
                    "message": f"cannot read .paper.number marker in {candidate.name}",
                })
                continue
            pn = str(data.get("paper_number") or "").strip()
            if not PAPER_NUMBER_RE.match(pn):
                blocking.append({
                    "dir": candidate.name,
                    "error": "invalid_paper_number",
                    "message": f"marker paper_number={pn!r} not valid 16-digit",
                })
                continue
            if pn in marker_map:
                blocking.append({
                    "dir": candidate.name,
                    "error": "duplicate_paper_number",
                    "message": f"paper_number={pn} claimed by {marker_map[pn]} and {candidate.name}",
                })
                continue
            marker_map[pn] = candidate.name
        else:
            # No marker — can't determine paper_number
            blocking.append({
                "dir": candidate.name,
                "error": "missing_marker",
                "message": f"no .paper.number marker in {candidate.name}",
            })

    if blocking:
        return [], blocking

    # Cross-validate with ledger
    items = (ledger.load().get("items") or {})
    for pn, dir_name in sorted(marker_map.items()):
        item = items.get(pn) or {}
        ledger_state = item.get("state") or ""
        ledger_paper_id = item.get("paper_id") or ""

        if ledger_state != "active":
            blocking.append({
                "paper_number": pn,
                "paper_id": dir_name,
                "error": "ledger_not_active",
                "message": f"ledger state={ledger_state!r}, expected active",
            })
            continue

        try:
            info = validate_formal_paper(papers_dir / dir_name)
        except Exception as exc:
            blocking.append({
                "paper_number": pn,
                "paper_id": dir_name,
                "error": "formal_validation_failed",
                "message": str(exc),
            })
            continue

        if info.get("paper_number") != pn or info.get("paper_id") != dir_name:
            blocking.append({
                "paper_number": pn,
                "paper_id": dir_name,
                "error": "formal_identity_mismatch",
                "message": f"formal: number={info.get('paper_number')} id={info.get('paper_id')}",
            })
            continue

        if ledger_paper_id and ledger_paper_id != dir_name:
            blocking.append({
                "paper_number": pn,
                "paper_id": dir_name,
                "error": "ledger_paper_id_mismatch",
                "message": f"ledger paper_id={ledger_paper_id!r} vs dir={dir_name}",
            })
            continue

        targets.append({
            "paper_number": pn,
            "paper_id": dir_name,
        })

    # Check for conflicting commit journals
    for t in targets:
        try:
            active = find_active_transaction_for_paper(
                transaction_root=transaction_root,
                paper_number=t["paper_number"],
                paper_raw_root=paper_raw_root,
                papers_root=papers_dir,
            )
        except RuntimeError:
            blocking.append({
                "paper_number": t["paper_number"],
                "paper_id": t["paper_id"],
                "error": "ambiguous_transaction",
                "message": "multiple active journals",
            })
            continue
        if active is not None and active[0] == "commit":
            blocking.append({
                "paper_number": t["paper_number"],
                "paper_id": t["paper_id"],
                "error": "active_commit_transaction",
                "message": "commit in progress",
            })

    # Recompute: remove blocked from valid
    blocked_numbers = {b.get("paper_number") for b in blocking}
    targets = [t for t in targets if t["paper_number"] not in blocked_numbers]

    return targets, blocking


def _write_report(report_path: str, report: RollbackReport) -> None:
    """Atomically write the rollback report as JSON."""
    from src.utils.atomic_io import atomic_write_json

    rp = Path(report_path)
    if rp.suffix.lower() != ".json":
        raise ValueError("report path must have a .json extension")
    rp.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(rp, report.to_dict(), indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Transactionally roll back formal papers to paper_raw"
    )
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument(
        "--paper-number",
        help="16-digit paper number to roll back",
    )
    target_group.add_argument(
        "--paper-id",
        help="Paper ID to roll back",
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

    if not (args.paper_number or args.paper_id or args.all_papers):
        parser.error("one of --paper-number, --paper-id, or --all-papers is required")

    mode: Literal["dry_run", "apply"] = "apply" if args.apply else "dry_run"
    report = RollbackReport(
        mode=mode,
        requested_target={
            "paper_number": args.paper_number or None,
            "paper_id": args.paper_id or None,
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
    if args.all_papers:
        targets, blocking = _discover_all_papers_targets(papers_dir, ledger, paper_raw_root=paper_raw_root, transaction_root=transaction_root)
        report.summary["discovered"] = len(targets) + len(blocking)
        report.summary["planned"] = len(targets)
        report.summary["blocking_errors"] = len(blocking)

        for b in blocking:
            report.add_paper_result(PaperResult(
                paper_number=b.get("paper_number", ""),
                paper_id=b.get("paper_id", b.get("dir", "")),
                status="blocked",
                reason_code=b.get("error", "unknown"),
                message=b.get("message", ""),
            ))
        for t in targets:
            report.add_paper_result(PaperResult(
                paper_number=t["paper_number"],
                paper_id=t["paper_id"],
                status="planned",
            ))

        if blocking and mode == "apply":
            print(f"[BLOCKED] {len(blocking)} blocking error(s); no rollback executed.", flush=True)
            for b in blocking:
                print(f"  {b.get('paper_number','')} {b.get('paper_id',b.get('dir',''))}: {b.get('error','')}", flush=True)
            if args.report:
                _write_report(args.report, report)
            return EXIT_BLOCKING

        if mode == "dry_run":
            print(f"[DRY-RUN] Discovered {len(targets)} formal paper(s) to roll back.", flush=True)
            if blocking:
                print(f"[DRY-RUN] {len(blocking)} blocking error(s) — cannot apply:", flush=True)
                for b in blocking:
                    print(f"  {b.get('paper_number','')} {b.get('paper_id',b.get('dir',''))}: {b.get('error','')}", flush=True)
            else:
                for t in targets:
                    print(f"  Would roll back: number={t['paper_number']}, paper_id={t['paper_id']}", flush=True)
            if args.report:
                _write_report(args.report, report)
            return EXIT_BLOCKING if blocking else EXIT_OK

        # apply mode, no blocking
        print(f"[APPLY] Rolling back {len(targets)} formal paper(s)...", flush=True)

        paper_numbers_or_ids: list[dict[str, str | None]] = [
            {"paper_number": t["paper_number"], "paper_id": None}
            for t in targets
        ]
    elif args.paper_number:
        paper_numbers_or_ids = [{"paper_number": args.paper_number, "paper_id": None}]
    elif args.paper_id:
        paper_numbers_or_ids = [{"paper_number": None, "paper_id": args.paper_id}]
    else:
        assert False, "unreachable"
    # ────────────────────────────────────────────────────────────────

    if mode == "dry_run" and not args.all_papers:
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
            elif item["paper_id"]:
                pid = item["paper_id"]
                if "/" in pid or "\\" in pid or ".." in pid:
                    print(f"[ERROR] invalid paper_id (contains path characters): {pid}", flush=True)
                    return EXIT_CLI_ERROR
                try:
                    pnum = _resolve_paper_number(
                        paper_id=pid,
                        papers_dir=papers_dir,
                        paper_raw_root=paper_raw_root,
                        transaction_root=transaction_root,
                        ledger=ledger,
                    )
                except Exception as exc:
                    print(f"[ERROR] paper_id not found: {pid} ({exc})", flush=True)
                    if args.report:
                        report.summary["blocking_errors"] = 1
                        report.add_paper_result(PaperResult(
                            paper_id=pid,
                            status="blocked",
                            reason_code="paper_id_not_found",
                            message=str(exc),
                        ))
                        _write_report(args.report, report)
                    return EXIT_CLI_ERROR
                pid_display = pid
            else:
                assert False, "unreachable"
            print(f"[DRY-RUN] Would roll back: number={pnum}, paper_id={pid_display}", flush=True)
            report.summary["discovered"] = len(paper_numbers_or_ids)
            report.summary["planned"] = len(paper_numbers_or_ids)
            report.add_paper_result(PaperResult(
                paper_number=pnum,
                paper_id=item["paper_id"] or "",
                status="planned",
            ))
        if args.report:
            _write_report(args.report, report)
        return EXIT_OK

    # ── Apply single or batch ─────────────────────────────────────
    results: list[PaperResult] = []
    report.summary["discovered"] = max(report.summary["discovered"], len(paper_numbers_or_ids))
    report.summary["planned"] = max(report.summary["planned"], len(paper_numbers_or_ids))

    for item in paper_numbers_or_ids:
        pnum = item["paper_number"]
        pid = item["paper_id"]
        label = f"number={pnum or '?'} paper_id={pid or '?'}"
        try:
            print(f"  Rolling back: {label}", flush=True)
            actual_number = rollback_formal_papers(
                papers_dir=papers_dir,
                paper_raw_root=paper_raw_root,
                transaction_root=transaction_root,
                ledger_path=ledger_path,
                catalog_root=catalog_root,
                paper_number=pnum,
                paper_id=pid,
            )
            results.append(PaperResult(
                paper_number=actual_number,
                paper_id=pid or "",
                status="completed",
            ))
            report.summary["completed"] += 1
            print(f"    → completed", flush=True)
        except RuntimeError as exc:
            msg = str(exc)
            results.append(PaperResult(
                paper_number=pnum or "",
                paper_id=pid or "",
                status="failed",
                reason_code="runtime_error",
                message=msg,
            ))
            report.summary["failed"] += 1
            print(f"    → FAILED: {msg}", flush=True)
            break  # stop on first failure
        except Exception as exc:
            msg = str(exc)
            results.append(PaperResult(
                paper_number=pnum or "",
                paper_id=pid or "",
                status="failed",
                reason_code="error",
                message=msg,
            ))
            report.summary["failed"] += 1
            print(f"    → FAILED: {msg}", flush=True)
            break

    # Mark remaining as not_started
    done = report.summary["completed"] + report.summary["failed"]
    if done < len(paper_numbers_or_ids):
        for item in paper_numbers_or_ids[done:]:
            results.append(PaperResult(
                paper_number=item["paper_number"] or "",
                paper_id=item["paper_id"] or "",
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


if __name__ == "__main__":
    sys.exit(main())
