"""Restore paper_raw Markdown/images from verified MinerU output cache."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import PAPER_RAW_DIR
from src.services.ingest_ids import validate_paper_raw_id
from src.services.mineru_output_cache import MinerUOutputCache
from src.ingest.paper_raw import PaperRawConverter


def _source_ids(root: Path, args) -> list[str]:
    if args.paper_number:
        return [validate_paper_raw_id(args.paper_number)]
    if args.paper_numbers:
        return [validate_paper_raw_id(x) for x in args.paper_numbers]
    if args.all:
        return sorted(p.name for p in root.iterdir() if p.is_dir() and p.name.isdigit() and len(p.name) == 16)
    raise ValueError("--paper-number, --paper-numbers, or --all is required")


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore paper_raw md/images from verified MinerU output cache.")
    parser.add_argument("--paper-number", default=None)
    parser.add_argument("--paper-numbers", nargs="+", default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--output-cache-dir", type=Path, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    write = args.apply and not args.dry_run
    source_ids = _source_ids(args.paper_raw_dir, args)
    converter = PaperRawConverter(args.paper_raw_dir)
    if args.output_cache_dir:
        converter.output_cache = MinerUOutputCache(args.output_cache_dir, cleaner=converter.cleaner)
    converter.reuse_output_cache = True

    items = []
    for source_id in source_ids:
        item = {
            "paper_number": source_id,
            "paper_raw_id": source_id,
            "applied": write,
        }
        try:
            inspection = converter.inspect_output_cache(source_id)
            item.update(inspection)
            if write:
                result = converter.convert(source_id, cache_only=True, skip_existing=False)
                item.update(result)
                item["status"] = "restored_from_output_cache" if result.get("success") else "failed"
            else:
                item["status"] = "restorable" if inspection.get("hit") else "not_restorable"
        except Exception as exc:
            item.update({"status": "failed", "error": str(exc)})
        items.append(item)

    payload = {
        "applied": write,
        "summary": {
            "planned": len(items),
            "restorable": sum(1 for i in items if i.get("status") == "restorable"),
            "restored_from_output_cache": sum(1 for i in items if i.get("status") == "restored_from_output_cache"),
            "failed": sum(1 for i in items if i.get("status") == "failed"),
            "not_restorable": sum(1 for i in items if i.get("status") == "not_restorable"),
        },
        "items": items,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if any(i.get("status") == "failed" for i in items) else 0


if __name__ == "__main__":
    raise SystemExit(main())
