"""Unsafe legacy renumber entrypoint.

This script has been retired. Use:
  python scripts/audit_paper_number_ledger.py --strict
  python scripts/reset_paper_number_ledger.py --compact-paper-raw --dry-run
"""
from __future__ import annotations


def main() -> int:
    print(
        "Refusing to run unsafe legacy renumber script. "
        "Use scripts/audit_paper_number_ledger.py and "
        "scripts/reset_paper_number_ledger.py instead."
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
