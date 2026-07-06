"""One-shot repair: update ledger folder_name / folder_path to point to numbered folders.

When a legacy workspace has been renamed to a 16-digit numbered folder, the ledger
entry's folder_name must be updated to match.  This script updates every ledger entry
where:
  - folder_name != paper_number (still pointing to a legacy name)
  - the numbered folder paper_raw/<paper_number>/ actually exists on disk
Safe to rerun.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import PAPER_NUMBER_LEDGER_PATH, PAPER_RAW_DIR


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair ledger folder_name to point to numbered folders.")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    ledger = json.loads(args.ledger_path.read_text(encoding="utf-8"))
    items = ledger.get("items", {})

    updated: list[str] = []
    skipped: list[str] = []

    for paper_number, item in items.items():
        folder_name = item.get("folder_name", "")
        if folder_name == paper_number:
            continue  # already correct

        numbered_path = args.paper_raw_dir / paper_number
        if not numbered_path.is_dir():
            skipped.append(f"{paper_number}: legacy \"{folder_name}\", numbered folder absent — skip")
            continue

        new_folder_name = paper_number
        new_folder_path = str(numbered_path.resolve()).replace("\\", "/")
        old_folder_name = folder_name
        old_folder_path = item.get("folder_path", "")

        if args.apply:
            item["folder_name"] = new_folder_name
            item["folder_path"] = new_folder_path

        updated.append(
            f"{paper_number}: \"{_trunc(old_folder_name)}\" → \"{new_folder_name}\""
        )

    if args.apply:
        # Preserve order: rewrite with same key order (items last)
        args.ledger_path.write_text(
            json.dumps(ledger, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print(f"Updated: {len(updated)}")
    for u in updated:
        print(f"  {u}")
    print(f"Skipped: {len(skipped)}")
    for s in skipped:
        print(f"  {s}")

    if not args.apply:
        print("\n[Dry run. Use --apply to write changes.]")
    else:
        print("\n[Applied.]")
    return 0


def _trunc(s: str, n: int = 50) -> str:
    return s if len(s) <= n else s[:47] + "..."


if __name__ == "__main__":
    raise SystemExit(main())
