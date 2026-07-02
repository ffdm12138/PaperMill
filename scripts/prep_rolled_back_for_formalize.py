"""Prepare rolled-back paper_raw folders for formalize.

For each folder under data/paper_raw/ with a 16-digit name and rolled_back note:
  - Create a minimal .conversion.json manifest (already converted)
  - Reserve the paper_number in the ledger (restore reserved state)
  - Set .import_status.json status = catalog_ready
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import PAPER_RAW_DIR, PAPER_NUMBER_LEDGER_PATH
from src.services.ingest_ids import PAPER_NUMBER_RE
from src.services.ingest_state import (
    CATALOG_READY,
    write_import_status,
    read_import_status,
)
from src.services.v2_library import (
    PaperNumberLedger,
    compute_sha256,
)


def main():
    raw_dir = Path(PAPER_RAW_DIR)
    ledger = PaperNumberLedger(PAPER_NUMBER_LEDGER_PATH)
    apply = "--apply" in sys.argv

    folders = sorted(p for p in raw_dir.iterdir() if p.is_dir())
    targets = []
    for folder in folders:
        if not PAPER_NUMBER_RE.match(folder.name):
            continue  # numbered-workspace gate only; legacy/untitled dedup is
            # handled by ingest_duplicate_guard.
        status = read_import_status(folder).get("status")
        note = read_import_status(folder).get("note", "")
        if "rolled back" not in note and status != CATALOG_READY:
            continue
        targets.append(folder)

    if not targets:
        print("No rolled-back folders found to prepare.")
        return

    for folder in targets:
        pn = folder.name
        print(f"\n{'='*60}")
        print(f"Processing: {pn}")

        # 1. Compute PDF sha256
        pdf_path = folder / f"{pn}.pdf"
        if not pdf_path.exists():
            # Try legacy naming: <paper_id>.pdf
            legacy_pdfs = list(folder.glob("*.pdf"))
            if legacy_pdfs:
                pdf_path = legacy_pdfs[0]
                print(f"  Found legacy PDF: {pdf_path.name}")
            else:
                print(f"  ERROR: No PDF found in {folder}")
                continue

        pdf_sha = compute_sha256(pdf_path)
        print(f"  PDF sha256: {pdf_sha[:16]}...")

        # 2. Create .conversion.json manifest
        conv_path = folder / f"{pn}.conversion.json"
        if not conv_path.exists():
            manifest = {
                "status": "converted",
                "pdf_sha256": pdf_sha,
                "backend": "hybrid",
                "method": "auto",
                "lang": "auto",
                "effort": "auto",
                "converted_at": "2026-07-01T00:00:00",
                "note": "manually generated for rolled-back paper from data/papers",
            }
            if apply:
                from src.utils.atomic_io import atomic_write_json
                atomic_write_json(conv_path, manifest, indent=2)
                print(f"  Created: {pn}.conversion.json")
            else:
                print(f"  [DRY-RUN] Would create: {pn}.conversion.json")
        else:
            print(f"  Skipped: {pn}.conversion.json already exists")

        # 3. Reserve number in ledger (preserve existing number)
        existing_marker = ledger.paper_number_from_marker(folder)
        if existing_marker:
            print(f"  Marker exists: {existing_marker}")
            # Check ledger state
            data = ledger.load()
            items = data.get("items", {})
            entry = items.get(pn, {})
            state = entry.get("state")
            if state == "reserved":
                print(f"  Ledger already reserved for {pn}")
            else:
                if apply:
                    from src.services.ingest_ids import validate_paper_raw_id
                    validate_paper_raw_id(pn)
                    ledger.reserve_specific_for_paper_raw(pn, folder)
                    print(f"  Reserved {pn} in ledger")
                else:
                    print(f"  [DRY-RUN] Would reserve {pn} in ledger")
        else:
            if apply:
                ledger.reserve_specific_for_paper_raw(pn, folder)
                print(f"  Created marker + reserved {pn} in ledger")
            else:
                print(f"  [DRY-RUN] Would create marker + reserve {pn}")

        # 4. Set status to catalog_ready
        current_status = read_import_status(folder).get("status")
        if current_status != CATALOG_READY:
            if apply:
                write_import_status(
                    folder,
                    CATALOG_READY,
                    reason="prepared from rolled-back data/papers",
                    extra={
                        "paper_number": pn,
                        "note": "prepared for formalize after rollback",
                        "rolled_back_at": None,
                    },
                )
                print(f"  Status set to: {CATALOG_READY}")
            else:
                print(f"  [DRY-RUN] Would set status: {CATALOG_READY}")
        else:
            print(f"  Already: {CATALOG_READY}")

    print(f"\n{'='*60}")
    print(f"Total: {len(targets)} folders")
    if not apply:
        print("Run with --apply to execute.")


if __name__ == "__main__":
    main()
