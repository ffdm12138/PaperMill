"""One-shot repair: rewrite corrupted .paper.number markers as valid JSON.

Corrupted markers contain a plain-text 16-digit paper_number (e.g. "0000000000000001")
instead of the expected JSON object.  This script reads the ledger to map folder_name →
paper_number, then rewrites every corrupted or missing marker in place.

Safe to rerun: valid JSON markers are left untouched.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import PAPER_NUMBER_LEDGER_PATH, PAPER_RAW_DIR


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair corrupted .paper.number markers.")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    ledger = json.loads(args.ledger_path.read_text(encoding="utf-8"))
    items = ledger.get("items", {})

    fixed: list[str] = []
    skipped_ok: list[str] = []
    errors: list[str] = []

    for paper_number, item in items.items():
        folder_name = item.get("folder_name", "")
        ws = args.paper_raw_dir / folder_name
        if not ws.is_dir():
            continue

        # Find .paper.number files
        markers = sorted(ws.glob("*.paper.number"))
        if not markers:
            # No marker at all — create one
            marker_path = ws / f"{paper_number}.paper.number"
            marker_content = {
                "paper_number": paper_number,
                "folder_name": folder_name,
                "state": item.get("state", "reserved"),
                "planned_paper_id": item.get("planned_paper_id", ""),
            }
            if args.apply:
                marker_path.write_text(
                    json.dumps(marker_content, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            fixed.append(f"{folder_name}: created {marker_path.name}")
            continue

        for mp in markers:
            raw = mp.read_text(encoding="utf-8").strip()
            try:
                json.loads(raw)
                skipped_ok.append(f"{folder_name}/{mp.name}: already valid JSON")
                continue  # already valid
            except (json.JSONDecodeError, ValueError):
                pass

            # Corrupted — raw is a plain paper_number string
            marker_content = {
                "paper_number": raw if raw.isdigit() and len(raw) == 16 else paper_number,
                "folder_name": folder_name,
                "state": item.get("state", "reserved"),
                "planned_paper_id": item.get("planned_paper_id", ""),
            }
            if args.apply:
                mp.write_text(
                    json.dumps(marker_content, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            fixed.append(f"{folder_name}/{mp.name}: {raw!r} → JSON")

    print(f"Fixed: {len(fixed)}")
    for f in fixed:
        print(f"  {f}")
    print(f"Skipped (already valid): {len(skipped_ok)}")
    if errors:
        print(f"Errors: {len(errors)}")
        for e in errors:
            print(f"  {e}")

    if not args.apply:
        print("\n[Dry run. Use --apply to write changes.]")
    else:
        print("\n[Applied.]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
