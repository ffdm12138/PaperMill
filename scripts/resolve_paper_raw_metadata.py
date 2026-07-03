"""Resolve metadata candidates for paper_raw folders whose metadata is unmatched.

Three-tier write semantics (do NOT let dry-run pollute paper_raw):
  - default / --dry-run: writes NOTHING, prints report JSON to stdout.
  - --write-candidates (without --apply): writes <id>.metadata.candidates.json,
    <id>.metadata.resolve_report.json, <id>.metadata.patch.json when a usable
    best candidate exists, and .import_status.json; does NOT touch metadata.json.
  - --apply (implies candidate/report writing): may modify <id>.metadata.json after gate/validation.
  - --report <path>: writes a summary JSON of all processed paper_numbers to <path> (any tier).

Network is OFF by default (--no-network). Use --allow-network for title search.

Rate limiting (--paper-interval-seconds / --provider-min-interval):
  When --allow-network is set, a ProviderRateLimiter enforces conservative
  spacing between papers and between provider requests, with 429/403/timeout
  backoff. Use --rate-probe --probe-size N to test a subset first.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from config.settings import ALL_CATALOG_PATH, PAPER_RAW_DIR, PAPERS_DIR
from src.services.ingest_ids import validate_paper_raw_id
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
from src.services.metadata_resolve_checkpoint import (
    load_checkpoint,
    record_item,
    save_checkpoint,
    is_done as checkpoint_is_done,
)
from src.services.rate_limit import ProviderRateLimiter, load_config as load_rate_config
from src.services.v2_library import metadata_doi, metadata_is_matched
from src.utils.atomic_io import atomic_write_json


def _source_ids(root: Path, all_unmatched: bool, all_papers: bool, one: str | None) -> list[str]:
    if one:
        return [validate_paper_raw_id(one)]
    if all_unmatched or all_papers:
        out = []
        for p in sorted(root.iterdir()):
            if not (p.is_dir() and p.name.isdigit() and len(p.name) == 16):
                continue
            meta_path = p / f"{p.name}.metadata.json"
            if not meta_path.exists():
                continue
            if all_unmatched:
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                status = ((meta.get("metadata_match") or {}).get("status") or "")
                if status == "unmatched":
                    out.append(p.name)
            else:
                # --all: process every workspace with a metadata file
                out.append(p.name)
        return out
    raise ValueError("--paper-number, --all-unmatched, or --all is required")


def _is_citation_ready(folder: Path, paper_number: str) -> bool:
    """True when metadata is already matched/manual_confirmed with a valid DOI."""
    meta_path = folder / f"{paper_number}.metadata.json"
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return metadata_is_matched(meta) and bool(metadata_doi(meta))


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


def _parse_provider_min_interval(value: str) -> tuple[str, float]:
    if "=" not in value:
        raise argparse.ArgumentTypeError(
            f"--provider-min-interval expects provider=seconds, got {value!r}")
    provider, seconds_str = value.split("=", 1)
    provider = provider.strip().lower()
    if not provider:
        raise argparse.ArgumentTypeError(f"empty provider in {value!r}")
    try:
        seconds = float(seconds_str)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid seconds in {value!r}")
    if seconds < 0:
        raise argparse.ArgumentTypeError(f"negative seconds in {value!r}")
    return provider, seconds


def _build_rate_limiter(args) -> ProviderRateLimiter | None:
    """Build a ProviderRateLimiter from config + CLI overrides, or None if network off."""
    if not args.allow_network:
        return None
    cfg = load_rate_config(args.rate_config)
    rl = ProviderRateLimiter(cfg)
    if args.paper_interval_seconds is not None:
        rl.set_paper_interval(args.paper_interval_seconds)
    for provider, seconds in (args.provider_min_interval or []):
        rl.set_provider_min_interval(provider, seconds)
    return rl


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve metadata candidates for v2 paper_raw folders.")
    parser.add_argument("--paper-number", default=None)
    parser.add_argument("--all-unmatched", action="store_true")
    parser.add_argument("--all", action="store_true",
                        help="process all paper_raw workspaces with a metadata file (not just unmatched)")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--all-catalog", type=Path, default=Path(ALL_CATALOG_PATH))
    parser.add_argument("--papers-dir", type=Path, default=Path(PAPERS_DIR))
    network = parser.add_mutually_exclusive_group()
    network.add_argument("--allow-network", action="store_true")
    network.add_argument("--no-network", action="store_true")
    parser.add_argument("--max-candidates", type=int, default=5)
    parser.add_argument("--min-confidence", type=float, default=0.70)
    parser.add_argument("--report", type=Path, default=None,
                        help="write a summary JSON of all processed paper_numbers to this path")
    parser.add_argument("--write-candidates", action="store_true",
                        help="write <id>.metadata.candidates.json + resolve_report.json + .import_status.json "
                             "(side files only; does not modify metadata.json unless --apply)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--manual-confirm", action="store_true")
    parser.add_argument("--candidate-id", default=None)
    parser.add_argument("--prefer-markdown", action="store_true",
                        help="when <paper_number>.md exists, prefer Markdown title/first-pages/abstract "
                             "evidence (post-conversion re-resolution); DOI priority is unchanged")
    # ── Rate limiting / checkpoint / probe ──
    parser.add_argument("--rate-config", type=Path, default=Path("config/metadata_rate_limits.json"),
                        help="path to the rate-limit config JSON")
    parser.add_argument("--paper-interval-seconds", type=float, default=None,
                        help="override global seconds between papers (default: 8.0 from config)")
    parser.add_argument("--provider-min-interval", action="append", default=[], type=_parse_provider_min_interval,
                        metavar="PROVIDER=SECONDS",
                        help="override a provider's min interval, e.g. crossref=3 (repeatable)")
    parser.add_argument("--rate-probe", action="store_true",
                        help="probe mode: respect --probe-size limit and do not write side files "
                             "(use with --dry-run to test a safe speed)")
    parser.add_argument("--probe-size", type=int, default=30,
                        help="max papers to process in --rate-probe mode (default 30)")
    parser.add_argument("--checkpoint", type=Path, default=None,
                        help="path to a checkpoint JSON for resume-after-429")
    parser.add_argument("--resume", action="store_true",
                        help="with --checkpoint: skip papers already matched/citation-ready in the checkpoint")
    parser.add_argument("--force", action="store_true",
                        help="re-resolve even when metadata is already citation-ready (matched + valid DOI)")
    args = parser.parse_args()

    allow_network = args.allow_network and not args.no_network
    # default dry-run: nothing written unless --write-candidates or --apply
    write_side_files = (args.apply or args.write_candidates) and not args.rate_probe
    apply_changes = args.apply and not args.dry_run and not args.rate_probe

    rate_limiter = _build_rate_limiter(args)

    try:
        source_ids = _source_ids(args.paper_raw_dir, args.all_unmatched, args.all, args.paper_number)
    except ValueError as exc:
        parser.error(str(exc))

    # --resume: skip citation-ready papers (checkpoint or live metadata)
    checkpoint_data = None
    if args.checkpoint:
        checkpoint_data = load_checkpoint(args.checkpoint)

    # --rate-probe: limit to probe_size papers
    if args.rate_probe:
        source_ids = source_ids[:max(0, args.probe_size)]

    items = []
    skipped_citation_ready = 0
    run_start = time.monotonic()

    for source_id in source_ids:
        folder = args.paper_raw_dir / source_id
        item = {"paper_number": source_id, "paper_raw_id": source_id, "status": "planned", "warnings": []}

        # Skip citation-ready metadata unless --force
        if not args.force and _is_citation_ready(folder, source_id):
            item["status"] = "skipped_citation_ready"
            items.append(item)
            skipped_citation_ready += 1
            logger.info("SKIP {} (citation-ready; use --force to re-resolve)", source_id)
            if checkpoint_data is not None:
                record_item(checkpoint_data, source_id, status="skipped", last_provider="")
            continue

        # --resume: skip papers already done in checkpoint
        if args.resume and checkpoint_data is not None and checkpoint_is_done(checkpoint_data, source_id):
            item["status"] = "skipped_checkpoint"
            items.append(item)
            skipped_citation_ready += 1
            continue

        if rate_limiter is not None:
            rate_limiter.begin_paper()

        try:
            report = resolve_metadata_candidates(
                folder,
                allow_network=allow_network,
                max_candidates=args.max_candidates,
                min_confidence=args.min_confidence,
                all_catalog_path=args.all_catalog,
                papers_dir=args.papers_dir,
                paper_raw_dir=args.paper_raw_dir,
                prefer_markdown=args.prefer_markdown,
                rate_limiter=rate_limiter,
            )
            report.post_conversion = args.prefer_markdown
            item.update({
                "decision": report.decision,
                "best_candidate_id": report.best_candidate_id,
                "candidate_count": len(report.candidates),
                "doi_source": report.doi_source,
                "used_markdown": report.used_markdown(),
                "metadata_sources": report.metadata_sources(),
                "post_conversion": report.post_conversion,
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
                        "paper_number": source_id,
                        "paper_raw_id": source_id,
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
                    paper_raw_dir=args.paper_raw_dir,
                )
                item.update(applied)
                item["status"] = applied.get("status", "applied") if applied.get("applied") else "manual_review_required"
            else:
                item["applied"] = False
                item["status"] = report.decision

            if checkpoint_data is not None:
                cp_status = "matched" if item.get("status") in {"matched", "manual_confirmed"} else (
                    "rate_limited" if rate_limiter and rate_limiter.stats.http_429_count > 0 else
                    "unmatched" if item.get("status") in {"manual_review", "rejected", "no_candidates"} else
                    "failed"
                )
                record_item(checkpoint_data, source_id, status=cp_status, last_provider="")

        except Exception as exc:
            item.update({"status": "failed", "error": str(exc)})
            logger.error("resolve failed for {}: {}", source_id, exc)
            if checkpoint_data is not None:
                record_item(checkpoint_data, source_id, status="failed", last_error=str(exc))
        items.append(item)

    # Save checkpoint after each run
    if checkpoint_data is not None:
        save_checkpoint(args.checkpoint, checkpoint_data)

    elapsed = time.monotonic() - run_start
    summary = {
        "total": len(source_ids),
        "processed": len(items),
        "skipped_citation_ready": skipped_citation_ready,
        "failed": sum(1 for i in items if i.get("status") == "failed"),
        "rate_limited": rate_limiter.stats.http_429_count if rate_limiter else 0,
        "elapsed_seconds": round(elapsed, 2),
        "effective_seconds_per_paper": round(elapsed / max(1, len(items)), 2) if items else 0,
    }
    if rate_limiter is not None:
        summary["rate_limit"] = rate_limiter.stats_dict()

    payload = {
        "applied": apply_changes,
        "rate_probe": args.rate_probe,
        "summary": summary,
        "items": items,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if any(i.get("status") == "failed" for i in items) else 0


if __name__ == "__main__":
    raise SystemExit(main())
