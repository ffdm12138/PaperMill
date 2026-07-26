"""Attach a local PDF to an existing data/paper_raw/<paper_number>/ folder."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import PAPER_RAW_DIR, PAPERS_DIR
from src.services.ingest_duplicate_guard import DuplicateIngestError
from src.utils.identifiers import validate_paper_raw_id
from src.ingest.paper_raw import PaperRawAllocator


def main() -> int:
    parser = argparse.ArgumentParser(description="Attach PDF to v2 paper_raw source folder.")
    parser.add_argument("pdf_path", type=Path)
    parser.add_argument("--paper-number", required=True)
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--copy", action="store_true")
    parser.add_argument("--move", action="store_true")
    parser.add_argument("--replace", action="store_true", help="explicitly replace an existing paper_raw PDF")
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
        try:
            out = PaperRawAllocator(args.paper_raw_dir, papers_dir=args.papers_dir).attach_pdf(
                paper_number, args.pdf_path, move=move, replace=args.replace
            )
            result.update(out)
            result["status"] = "attached"
        except DuplicateIngestError as exc:
            result.update({
                "status": "duplicate",
                "error": "pdf_duplicate",
                "duplicate_reasons": exc.result.reasons,
                "duplicate_refs": [ref.to_dict() for ref in exc.result.refs],
                "pdf_md5": exc.result.pdf_md5,
                "pdf_sha256": exc.result.pdf_sha256,
            })
        except Exception as exc:
            result.update({"status": "failed", "error": str(exc)})
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["status"] in {"duplicate", "failed"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
