"""Attach a local PDF to an existing data/paper_raw/<paper_number>/ folder."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import PAPER_RAW_DIR
from src.services.ingest_ids import validate_paper_raw_id
from src.services.v2_library import PaperRawAllocator


def main() -> int:
    parser = argparse.ArgumentParser(description="Attach PDF to v2 paper_raw source folder.")
    parser.add_argument("pdf_path", type=Path)
    parser.add_argument("--paper-number", required=True)
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--copy", action="store_true")
    parser.add_argument("--move", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    paper_number = validate_paper_raw_id(args.paper_number)

    write = args.apply and not args.dry_run
    move = args.move and not args.copy
    folder = args.paper_raw_dir / paper_number
    dest = folder / f"{paper_number}.pdf"
    result = {
        "applied": write,
        "paper_number": paper_number,
        "paper_raw_id": paper_number,
        "source_pdf": str(args.pdf_path),
        "target_pdf": str(dest),
        "status": "planned",
    }
    if write:
        out = PaperRawAllocator(args.paper_raw_dir).attach_pdf(paper_number, args.pdf_path, move=move)
        result.update(out)
        result["status"] = "attached"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
