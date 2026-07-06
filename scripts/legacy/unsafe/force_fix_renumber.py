"""Unsafe legacy force-fix entrypoint."""
from __future__ import annotations


def main() -> int:
    print(
        "Refusing to run unsafe legacy force-fix script. "
        "Use scripts/audit_paper_number_ledger.py and "
        "scripts/reset_paper_number_ledger.py instead."
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
