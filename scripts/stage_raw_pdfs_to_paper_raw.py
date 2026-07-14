"""Stage root data/raw/*.pdf files into 16-digit paper_raw workspaces."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from config.settings import PAPER_NUMBER_LEDGER_PATH, PAPER_RAW_DIR, PAPERS_DIR, RAW_DIR
from src.file_fingerprint import compute_file_hashes
from src.services.ingest_duplicate_guard import DuplicateIngestError, check_pdf_duplicate
from src.library.paper_number_ledger import PaperNumberLedger
from src.ingest.paper_raw import PaperRawAllocator


def _is_pdf(path: Path) -> bool:
    try:
        return path.read_bytes()[:5].startswith(b"%PDF")
    except OSError:
        return False


def _active_workspace_count(paper_raw_dir: Path) -> int:
    from src.services.ingest_duplicate_guard import is_paper_raw_workspace
    if not paper_raw_dir.exists():
        return 0
    return sum(
        1 for f in paper_raw_dir.iterdir()
        if f.is_dir() and f.name != "quarantine" and not f.name.startswith(".") and is_paper_raw_workspace(f)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage data/raw/*.pdf into v2 paper_raw workspaces.")
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--copy", action="store_true", help="copy PDFs into paper_raw (default)")
    parser.add_argument("--move", action="store_true", help="move PDFs into paper_raw")
    parser.add_argument("--apply", action="store_true", help="write changes; default is dry-run")
    parser.add_argument("--dry-run", action="store_true", help="force dry-run")
    parser.add_argument("--expect-final-count", type=int, default=None,
                        help="expected active workspace count after staging (active + planned_new)")
    parser.add_argument("--refuse-if-final-count-mismatch", action="store_true",
                        help="with --expect-final-count, refuse apply when active+planned_new != expect")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    write = args.apply and not args.dry_run
    pdfs = sorted(p for p in args.raw_dir.glob("*.pdf") if p.is_file())
    active_count = _active_workspace_count(args.paper_raw_dir)
    # Planned paper numbers are assigned LAZILY to non-duplicate PDFs only.
    # Pre-allocating for all PDFs (including duplicates) made dry-run number
    # ranges misleading (e.g. 0206->0358 when only a subset would actually be
    # staged). The ledger is only peeked, never mutated, in dry-run.
    start_number = int(str(PaperNumberLedger(args.ledger_path).load().get("max_number") or "0")) + 1
    report: list[dict] = []
    allocator = PaperRawAllocator(args.paper_raw_dir, ledger_path=args.ledger_path, papers_dir=args.papers_dir)
    if args.copy and args.move:
        parser.error("--copy and --move are mutually exclusive")
    move = bool(args.move)
    operation = "move" if move else "copy"

    if write and not move:
        warning = (
            "WARNING: Manual PDF staging is running in copy mode.\n"
            "data/raw PDFs will remain in place.\n"
            "Normal manual ingest SOP is --move --apply so data/raw behaves as a queue.\n"
            "Use --move for normal ingestion, or keep copy mode only for debugging/backup/tests."
        )
        print(warning, file=sys.stderr)
        logger.warning(warning.replace("\n", " "))
    elif not write:
        print(f"DRY RUN: staging mode = {operation}", file=sys.stderr)

    seen_batch_sha: dict[str, Path] = {}
    seen_batch_md5: dict[str, Path] = {}
    duplicate_count = 0
    failed_count = 0
    planned_items: list[dict] = []
    # Phase 1 — classify every PDF (failed/duplicate/planned) WITHOUT staging.
    # Staging is deferred so --expect-final-count can refuse apply before any
    # workspace is created or any source PDF is moved.
    for pdf in pdfs:
        item = {
            "source_pdf": str(pdf),
            "operation": operation,
            "staging_mode": operation,
            "move": move,
            "status": "planned",
        }
        if not _is_pdf(pdf):
            item.update({"status": "failed", "error": "file does not look like a PDF"})
            logger.warning("{} skipped: {}", pdf, item["error"])
            report.append(item)
            failed_count += 1
            continue
        hashes = compute_file_hashes(pdf)
        item["original_path"] = str(pdf)
        item["original_md5"] = hashes["md5"]
        item["original_sha256"] = hashes["sha256"]
        item["original_file_size"] = hashes["file_size"]
        batch_refs = []
        batch_reasons = []
        if hashes["sha256"] in seen_batch_sha:
            batch_reasons.append("batch_pdf_duplicate")
            batch_reasons.append("pdf_sha256_duplicate")
            batch_refs.append({
                "scope": "batch",
                "paper_number": "",
                "paper_name": "",
                "folder": str(seen_batch_sha[hashes["sha256"]]),
                "source": "input_pdf",
                "doi": "",
                "pdf_md5": "",
                "pdf_sha256": hashes["sha256"],
            })
        if hashes["md5"] in seen_batch_md5:
            batch_reasons.append("batch_pdf_duplicate")
            batch_reasons.append("pdf_md5_duplicate")
            batch_refs.append({
                "scope": "batch",
                "paper_number": "",
                "paper_name": "",
                "folder": str(seen_batch_md5[hashes["md5"]]),
                "source": "input_pdf",
                "doi": "",
                "pdf_md5": hashes["md5"],
                "pdf_sha256": "",
            })
        dup = check_pdf_duplicate(pdf, paper_raw_dir=args.paper_raw_dir, papers_dir=args.papers_dir)
        reasons = list(dict.fromkeys([*batch_reasons, *dup.reasons]))
        refs = [*batch_refs, *[ref.to_dict() for ref in dup.refs]]
        if reasons:
            item.update({
                "status": "duplicate",
                "error": "pdf_duplicate",
                "duplicate_reasons": reasons,
                "duplicate_refs": refs,
            })
            logger.warning("{} duplicate: {}", pdf, ", ".join(reasons))
            report.append(item)
            duplicate_count += 1
            continue
        seen_batch_sha[hashes["sha256"]] = pdf
        seen_batch_md5[hashes["md5"]] = pdf
        planned_items.append(item)
        report.append(item)

    planned_new_count = len(planned_items)
    planned_number_start = f"{start_number:016d}" if planned_new_count else ""
    planned_number_end = f"{start_number + planned_new_count - 1:016d}" if planned_new_count else ""
    expected_final_count = active_count + planned_new_count

    # --expect-final-count guard: refuse apply if the projected final active
    # count does not match. Protects against stacking a raw queue on top of a
    # paper_raw that already holds workspaces not backed by raw. Evaluated
    # BEFORE any staging so a refused import leaves paper_raw untouched.
    count_mismatch = (
        args.expect_final_count is not None
        and expected_final_count != args.expect_final_count
    )
    refuse_apply = count_mismatch and args.refuse_if_final_count_mismatch
    if refuse_apply and write:
        msg = (
            f"refusing import: active_count={active_count} "
            f"+ planned_new_count={planned_new_count} = {expected_final_count} "
            f"!= expect_final_count={args.expect_final_count}"
        )
        print(msg, file=sys.stderr)
        logger.error(msg)

    # Phase 2 — execute. Dry-run assigns planned numbers to the planned items;
    # apply stages them via the allocator (which re-checks duplicates against
    # the live paper_raw, so a race or concurrent change surfaces as a dup).
    do_stage = write and not refuse_apply
    for index, item in enumerate(planned_items):
        pdf = Path(item["source_pdf"])
        if not do_stage:
            planned_id = f"{start_number + index:016d}"
            item["planned_paper_number"] = planned_id
            item["planned_paper_raw_id"] = planned_id
            logger.info("{} {} -> paper_raw/{}", "DRY-RUN", pdf.name, planned_id)
            continue
        try:
            result = allocator.allocate_from_pdf(pdf, source_type="manual_pdf", move=move)
            item.update(result)
            manifest_path = Path(result["folder"]) / "stage_manifest.json"
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                staged_pdf = manifest.get("staged_pdf") or {}
                item["staged_path"] = staged_pdf.get("path", result.get("pdf", ""))
                item["staged_md5"] = staged_pdf.get("md5", "")
                item["staged_sha256"] = staged_pdf.get("sha256", "")
            item["status"] = "staged"
            logger.info("{} {} -> paper_raw/{}", "STAGE", pdf.name, result["paper_number"])
        except DuplicateIngestError as exc:
            item.update({
                "status": "duplicate",
                "error": "pdf_duplicate",
                "duplicate_reasons": exc.result.reasons,
                "duplicate_refs": [ref.to_dict() for ref in exc.result.refs],
            })
            duplicate_count += 1
        except Exception as exc:
            item.update({"status": "failed", "error": str(exc)})
            failed_count += 1
            logger.error("stage failed for {}: {}", pdf, exc)

    summary = {
        "raw_pdf_count": len(pdfs),
        "active_count": active_count,
        "duplicate_count": duplicate_count,
        "failed_count": failed_count,
        "planned_new_count": planned_new_count,
        "planned_number_start": planned_number_start,
        "planned_number_end": planned_number_end,
        "expected_final_count": expected_final_count,
        "expect_final_count": args.expect_final_count,
        "count_mismatch": count_mismatch,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"applied": write and not refuse_apply, "summary": summary, "count": len(report), "items": report}, ensure_ascii=False, indent=2))
    if refuse_apply and write:
        return 1
    return 1 if any(i["status"] in {"failed", "duplicate"} for i in report) else 0


if __name__ == "__main__":
    raise SystemExit(main())
