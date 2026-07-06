"""Rejected legacy rollback entrypoint.

The old implementation was non-transactional and could corrupt the paper number
ledger. Use ``rollback_formal_papers_to_paper_raw.py`` instead.
"""

raise SystemExit(
    "rollback_papers_to_raw.py is unsafe legacy. "
    "Use scripts/rollback_formal_papers_to_paper_raw.py instead."
)
