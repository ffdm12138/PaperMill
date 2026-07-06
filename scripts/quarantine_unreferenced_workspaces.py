"""Move legacy paper_raw workspaces not referenced by the ledger into quarantine.

Safe: moves (never deletes) into data/paper_raw/quarantine/unreferenced_workspaces/.
Only targets folders whose name is NOT a 16-digit paper_number AND whose name is
NOT listed as any ledger item's folder_name.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import PAPER_NUMBER_LEDGER_PATH, PAPER_RAW_DIR


def main() -> int:
    parser = argparse.ArgumentParser(description="Quarantine unreferenced legacy workspaces.")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    ledger = json.loads(args.ledger_path.read_text(encoding="utf-8"))
    ledger_folders: set[str] = {item["folder_name"] for item in ledger.get("items", {}).values()}

    quarantine_root = args.paper_raw_dir / "quarantine" / "unreferenced_workspaces"

    to_move: list[tuple[Path, Path, float]] = []
    skipped: list[str] = []

    for d in sorted(args.paper_raw_dir.iterdir()):
        if not d.is_dir():
            continue
        if d.name == "quarantine":
            continue
        if len(d.name) == 16 and d.name.isdigit():
            continue  # numbered folder
        if d.name in ledger_folders:
            continue  # still referenced

        total_size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        target = quarantine_root / d.name
        to_move.append((d, target, total_size))

    total_size_mb = sum(s for _, _, s in to_move) / 1024 / 1024

    print(f"Unreferenced legacy workspaces to quarantine: {len(to_move)} ({total_size_mb:.1f} MB)")
    for src, dst, size in to_move:
        print(f"  {src.name} → {dst} ({size / 1024:.0f} KB)")

    if args.apply:
        quarantine_root.mkdir(parents=True, exist_ok=True)
        moved = 0
        for src, dst, size in to_move:
            if dst.exists():
                print(f"  SKIP {src.name}: already in quarantine")
                continue
            shutil.move(str(src), str(dst))
            moved += 1
            print(f"  MOVED {src.name}")
        print(f"\nQuarantined: {moved} workspaces → {quarantine_root}")

        # Write manifest
        manifest = {
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "quarantine_root": str(quarantine_root),
            "count": moved,
            "entries": [
                {"name": src.name, "target": str(dst)} for src, dst, _ in to_move
            ],
        }
        manifest_path = quarantine_root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        print(f"\n[Dry run. Use --apply to move {len(to_move)} workspaces to quarantine.]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
