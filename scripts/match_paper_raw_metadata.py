"""Legacy compatibility wrapper for the canonical paper_raw metadata resolver."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import ALL_CATALOG_PATH, PAPER_RAW_DIR, PAPERS_DIR
from src.services.metadata_resolver import (
    STATUS_CANDIDATE_CONFLICT,
    STATUS_CANDIDATES_FOUND,
    STATUS_MANUAL_REVIEW,
    STATUS_RESOLVE_FAILED,
    apply_resolution,
    resolve_metadata_candidates,
    write_candidates_json,
    write_metadata_patch_json,
    write_resolve_report_json,
)
from src.utils.atomic_io import atomic_write_json


LEGACY_NOTICE = (
    "match_paper_raw_metadata.py is a legacy compatibility wrapper; "
    "use resolve_paper_raw_metadata.py as the canonical resolver."
)


def _source_ids(root: Path, all_sources: bool, one: str | None) -> list[str]:
    if one:
        return [one]
    if all_sources:
        return sorted(
            p.name for p in root.iterdir()
            if p.is_dir() and p.name.isdigit() and len(p.name) == 6
        )
    raise ValueError("--source-id or --all is required")


def _status_for_report(report) -> str:
    if report.decision == "conflict":
        return STATUS_CANDIDATE_CONFLICT
    if report.decision in {"no_candidates", "rejected"}:
        return STATUS_RESOLVE_FAILED
    if report.decision == "manual_review":
        return STATUS_MANUAL_REVIEW
    return STATUS_CANDIDATES_FOUND


def _write_report_status(folder: Path, report) -> None:
    atomic_write_json(folder / ".import_status.json", {
        "status": _status_for_report(report),
        "source_id": report.source_id,
        "best_decision": report.decision,
        "reason": report.reason,
        "created_at": report.created_at,
    }, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Legacy wrapper for canonical v2 paper_raw metadata resolver.")
    parser.add_argument("--source-id", default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--all-catalog", type=Path, default=Path(ALL_CATALOG_PATH))
    parser.add_argument("--papers-dir", type=Path, default=Path(PAPERS_DIR))
    parser.add_argument("--manual-confirm", action="store_true")
    parser.add_argument("--candidate-id", default=None)
    parser.add_argument("--require-matched", action="store_true",
                        help="return non-zero if any processed source remains unmatched")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    print(LEGACY_NOTICE, file=sys.stderr)

    write = args.apply and not args.dry_run
    items = []
    for source_id in _source_ids(args.paper_raw_dir, args.all, args.source_id):
        folder = args.paper_raw_dir / source_id
        item = {"source_id": source_id, "status": "planned", "legacy_wrapper": True, "warnings": []}
        try:
            report = resolve_metadata_candidates(
                folder,
                allow_network=True,
                all_catalog_path=args.all_catalog,
                papers_dir=args.papers_dir,
            )
            item.update({
                "decision": report.decision,
                "best_candidate_id": report.best_candidate_id,
                "candidate_count": len(report.candidates),
                "doi_source": report.doi_source,
                "warnings": report.warnings,
            })

            if write:
                write_candidates_json(folder, report)
                write_resolve_report_json(folder, report)
                write_metadata_patch_json(folder, report)
                if report.decision == "conflict":
                    _write_report_status(folder, report)
                    item["status"] = "unmatched"
                    item["applied"] = False
                else:
                    applied = apply_resolution(
                        folder,
                        report,
                        manual_confirm=args.manual_confirm,
                        candidate_id=args.candidate_id,
                        all_catalog_path=args.all_catalog,
                        papers_dir=args.papers_dir,
                    )
                    item.update(applied)
                    item["status"] = (
                        applied.get("status", "applied")
                        if applied.get("applied")
                        else "manual_review_required"
                    )
            else:
                item["status"] = report.decision
                item["applied"] = False
        except Exception as exc:
            item.update({"status": "failed", "error": str(exc)})
        items.append(item)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"applied": write, "legacy_wrapper": True, "items": items}, ensure_ascii=False, indent=2))
    if any(i["status"] == "failed" for i in items):
        return 1
    if args.require_matched and any(i.get("status") not in {"matched", "manual_confirmed"} for i in items):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
