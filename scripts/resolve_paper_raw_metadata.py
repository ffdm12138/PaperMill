"""Resolve metadata candidates for paper_raw folders whose metadata is unmatched.

Three-tier write semantics (do NOT let dry-run pollute paper_raw):
  - default / --dry-run: writes NOTHING, prints report JSON to stdout.
  - --write-candidates (without --apply): writes <id>.metadata.candidates.json,
    <id>.metadata.resolve_report.json, <id>.metadata.patch.json when a usable
    best candidate exists, and .import_status.json; does NOT touch metadata.json.
  - --apply (implies candidate/report writing): may modify <id>.metadata.json after gate/validation.
  - --report <path>: writes a summary JSON of all processed source_ids to <path> (any tier).

Network is OFF by default (--no-network). Use --allow-network for title search.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from config.settings import ALL_CATALOG_PATH, PAPER_RAW_DIR, PAPERS_DIR
from src.services.metadata_resolver import (
    apply_resolution,
    resolve_metadata_candidates,
    write_candidates_json,
    write_metadata_patch_json,
    write_resolve_report_json,
    STATUS_CANDIDATES_FOUND,
    STATUS_CANDIDATE_CONFLICT,
    STATUS_MANUAL_REVIEW,
    STATUS_RESOLVE_FAILED,
)
from src.utils.atomic_io import atomic_write_json


def _source_ids(root: Path, all_unmatched: bool, one: str | None) -> list[str]:
    if one:
        return [one]
    if all_unmatched:
        out = []
        for p in sorted(root.iterdir()):
            if not (p.is_dir() and p.name.isdigit() and len(p.name) in (6, 16)):
                continue
            meta_path = p / f"{p.name}.metadata.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            status = ((meta.get("metadata_match") or {}).get("status") or "")
            if status == "unmatched":
                out.append(p.name)
        return out
    raise ValueError("--source-id or --all-unmatched is required")


def _import_status_for_report(report) -> str:
    if report.decision == "conflict":
        return STATUS_CANDIDATE_CONFLICT
    if report.decision == "no_candidates":
        return STATUS_RESOLVE_FAILED
    if not report.candidates:
        return STATUS_RESOLVE_FAILED
    if report.decision == "rejected":
        return STATUS_RESOLVE_FAILED
    return STATUS_CANDIDATES_FOUND


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve metadata candidates for v2 paper_raw folders.")
    parser.add_argument("--source-id", default=None)
    parser.add_argument("--all-unmatched", action="store_true")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--all-catalog", type=Path, default=Path(ALL_CATALOG_PATH))
    parser.add_argument("--papers-dir", type=Path, default=Path(PAPERS_DIR))
    network = parser.add_mutually_exclusive_group()
    network.add_argument("--allow-network", action="store_true")
    network.add_argument("--no-network", action="store_true")
    parser.add_argument("--max-candidates", type=int, default=5)
    parser.add_argument("--min-confidence", type=float, default=0.70)
    parser.add_argument("--report", type=Path, default=None,
                        help="write a summary JSON of all processed source_ids to this path")
    parser.add_argument("--write-candidates", action="store_true",
                        help="write <id>.metadata.candidates.json + resolve_report.json + .import_status.json "
                             "(side files only; does not modify metadata.json unless --apply)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--manual-confirm", action="store_true")
    parser.add_argument("--candidate-id", default=None)
    args = parser.parse_args()

    allow_network = args.allow_network and not args.no_network
    # default dry-run: nothing written unless --write-candidates or --apply
    write_side_files = args.apply or args.write_candidates
    apply_changes = args.apply and not args.dry_run

    items = []
    for source_id in _source_ids(args.paper_raw_dir, args.all_unmatched, args.source_id):
        folder = args.paper_raw_dir / source_id
        item = {"source_id": source_id, "status": "planned", "warnings": []}
        try:
            report = resolve_metadata_candidates(
                folder,
                allow_network=allow_network,
                max_candidates=args.max_candidates,
                min_confidence=args.min_confidence,
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

            if write_side_files:
                write_candidates_json(folder, report)
                write_resolve_report_json(folder, report)
                write_metadata_patch_json(folder, report)
                # write import_status marker (report-only tier)
                if not apply_changes:
                    status = _import_status_for_report(report)
                    atomic_write_json(folder / ".import_status.json", {
                        "status": status,
                        "source_id": source_id,
                        "best_decision": report.decision,
                        "reason": report.reason,
                        "created_at": report.created_at,
                    }, indent=2)

            if apply_changes:
                applied = apply_resolution(
                    folder, report,
                    manual_confirm=args.manual_confirm,
                    candidate_id=args.candidate_id,
                    all_catalog_path=args.all_catalog,
                    papers_dir=args.papers_dir,
                )
                item.update(applied)
                item["status"] = applied.get("status", "applied") if applied.get("applied") else "manual_review_required"
            else:
                item["applied"] = False
                item["status"] = report.decision
        except Exception as exc:
            item.update({"status": "failed", "error": str(exc)})
            logger.error("resolve failed for {}: {}", source_id, exc)
        items.append(item)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"applied": apply_changes, "items": items}, ensure_ascii=False, indent=2))
    return 1 if any(i.get("status") == "failed" for i in items) else 0


if __name__ == "__main__":
    raise SystemExit(main())
