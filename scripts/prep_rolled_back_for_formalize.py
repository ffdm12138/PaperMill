"""Rejected legacy rolled-back preparation entrypoint.

The old implementation wrote ``catalog_ready`` and synthetic conversion
manifests without validating the current rollback state.
"""

raise SystemExit(
    "prep_rolled_back_for_formalize.py is unsafe legacy. "
    "Use scripts/validate_rolled_back_paper_raw.py, regenerate catalog, then formalize."
)
