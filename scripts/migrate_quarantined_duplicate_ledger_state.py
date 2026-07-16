#!/usr/bin/env python
"""Explicit migration for the retired quarantined_duplicate ledger state."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import PAPER_NUMBER_LEDGER_PATH
from src.library.paper_number_ledger import PaperNumberLedger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    ledger = PaperNumberLedger(args.ledger)
    data = ledger.load()
    numbers = sorted(number for number, item in data.get("items", {}).items()
                     if isinstance(item, dict) and item.get("state") == "quarantined_duplicate")
    migrated: list[str] = []
    if args.apply:
        for number in numbers:
            ledger.migrate_legacy_quarantined_duplicate(number)
            migrated.append(number)
    print(json.dumps({"legacy_numbers": numbers, "applied": args.apply,
                      "migrated": migrated}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
