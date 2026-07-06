"""Compatibility wrapper for the rolled-back paper_raw validator."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import ALL_CATALOG_PATH, PAPER_NUMBER_LEDGER_PATH, PAPER_RAW_DIR, PAPERS_DIR
from scripts.validate_rolled_back_paper_raw import validate_rolled_back_state


def main() -> int:
    print(
        "validate_metadata_only_assets.py is deprecated; "
        "use scripts/validate_rolled_back_paper_raw.py for rollback validation."
    )
    errors, warnings, states = validate_rolled_back_state(
        papers_dir=PAPERS_DIR,
        paper_raw_dir=PAPER_RAW_DIR,
        ledger_path=PAPER_NUMBER_LEDGER_PATH,
        all_catalog_path=ALL_CATALOG_PATH,
    )
    valid = not errors
    print(f"valid={'True' if valid else 'False'} errors={len(errors)} warnings={len(warnings)}")
    if states:
        print("\nMetadata states:")
        for state in states:
            print(
                "  "
                f"{state['path']}: "
                f"schema_valid={state['schema_valid']} "
                f"citation_ready={state['citation_ready']} "
                f"matched_consistent={state['matched_consistent']}"
            )
    if errors:
        print("\nErrors:")
        for error in errors:
            print(f"  {error}")
    if warnings:
        print("\nWarnings:")
        for warning in warnings:
            print(f"  {warning}")
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
