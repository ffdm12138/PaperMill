"""Stage root data/raw/*.pdf files into 16-digit paper_raw workspaces."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from config.settings import PAPER_NUMBER_LEDGER_PATH, PAPER_RAW_DIR, RAW_DIR
from src.file_fingerprint import compute_sha256
from src.services.v2_library import PaperNumberLedger, PaperRawAllocator


def _is_pdf(path: Path) -> bool:
    try:
        return path.read_bytes()[:5].startswith(b"%PDF")
    except OSError:
        return False


def _planned_paper_numbers(ledger_path: Path, count: int) -> list[str]:
    return PaperNumberLedger(ledger_path).peek_next_numbers(count)


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage data/raw/*.pdf into v2 paper_raw workspaces.")
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--copy", action="store_true", help="copy PDFs into paper_raw (default)")
    parser.add_argument("--move", action="store_true", help="move PDFs into paper_raw")
    parser.add_argument("--apply", action="store_true", help="write changes; default is dry-run")
    parser.add_argument("--dry-run", action="store_true", help="force dry-run")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    write = args.apply and not args.dry_run
    pdfs = sorted(p for p in args.raw_dir.glob("*.pdf") if p.is_file())
    ids = _planned_paper_numbers(args.ledger_path, len(pdfs))
    report: list[dict] = []
    allocator = PaperRawAllocator(args.paper_raw_dir, ledger_path=args.ledger_path)
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

    for pdf, planned_id in zip(pdfs, ids):
        item = {
            "source_pdf": str(pdf),
            "planned_paper_number": planned_id,
            "planned_paper_raw_id": planned_id,
            "operation": operation,
            "staging_mode": operation,
            "move": move,
            "status": "planned",
        }
        if not _is_pdf(pdf):
            item.update({"status": "failed", "error": "file does not look like a PDF"})
            logger.warning("{} skipped: {}", pdf, item["error"])
            report.append(item)
            continue
        original_sha = compute_sha256(pdf)
        item["original_path"] = str(pdf)
        item["original_sha256"] = original_sha
        if write:
            try:
                result = allocator.allocate_from_pdf(pdf, source_type="manual_pdf", move=move)
                item.update(result)
                manifest_path = Path(result["folder"]) / "stage_manifest.json"
                if manifest_path.exists():
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    item["staged_path"] = manifest.get("staged_path", result.get("pdf", ""))
                    item["staged_sha256"] = manifest.get("staged_sha256", "")
                item["status"] = "staged"
            except Exception as exc:
                item.update({"status": "failed", "error": str(exc)})
                logger.error("stage failed for {}: {}", pdf, exc)
        logger.info("{} {} -> paper_raw/{}", "STAGE" if write else "DRY-RUN", pdf.name, planned_id)
        report.append(item)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"applied": write, "count": len(report), "items": report}, ensure_ascii=False, indent=2))
    return 1 if any(i["status"] == "failed" for i in report) else 0


if __name__ == "__main__":
    raise SystemExit(main())
