"""Audit formal metadata quality without mutating paper assets."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import CATALOG_STATE_ROOT, PAPERS_DIR
from src.services.metadata_quality import audit_metadata_library
from src.utils.atomic_io import atomic_write_json


DEFAULT_REPORT_PATH = CATALOG_STATE_ROOT / "metadata_quality_report.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit formal metadata quality.")
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--report", action="store_true", help="write data/catalog/metadata_quality_report.json")
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH)
    args = parser.parse_args(argv)

    report = audit_metadata_library(args.papers_dir)
    if args.report:
        atomic_write_json(args.report_path, report, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
