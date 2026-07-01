"""Rebuild v2 data/catalog/all.catalog.json from data/papers."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import (
    ALL_CATALOG_PATH,
    PAPER_NUMBER_LEDGER_PATH,
    PAPERS_DIR,
)
from src.services.v2_library import AllCatalogBuilder, PaperNumberLedger


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild v2 all.catalog.json.")
    parser.add_argument("--apply", action="store_true", help="write all.catalog.json; default is dry-run")
    parser.add_argument("--dry-run", action="store_true", help="print summary without writing")
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH,
                        help="paper_number_ledger.json path (tests/agents must pass tmp)")
    parser.add_argument("--all-catalog-path", type=Path, default=ALL_CATALOG_PATH,
                        help="all.catalog.json path (tests/agents must pass tmp)")
    args = parser.parse_args()
    write = args.apply and not args.dry_run
    ledger = PaperNumberLedger(args.ledger_path)
    builder = AllCatalogBuilder(
        papers_dir=args.papers_dir,
        all_catalog_path=args.all_catalog_path,
        ledger=ledger,
    )
    data = builder.build(write=write)
    for error in builder.last_errors:
        print(f"ERROR: {error}")
    print(f"papers={len(data.get('papers', []))} written={write}")
    return 1 if builder.last_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
