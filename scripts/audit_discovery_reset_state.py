#!/usr/bin/env python
"""Read-only, complete diagnostics for discovery reset state.

The audit never repairs state and never follows a symlink while inspecting the
audited tree.  Every component emits findings independently; readiness is
derived only after all component checks have run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.timestamps import utc_now_iso  # noqa: E402

# The audit engines live in src/discovery/audits/reset_state.py; this script
# is the argparse wiring.  ``_audit_locks`` and ``_snapshot_paths`` are
# re-exported for existing importers of this script module.
from src.discovery.audits.reset_state import (  # noqa: E402,F401
    DEFAULT_DATA,
    _audit_locks,
    _snapshot_paths,
    _write_report_atomically,
    audit_reset_state,
    probe_existing_file_lock,
    resolve_safe_report_path,
)


def _now() -> str:
    return utc_now_iso()


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only discovery reset-state audit")
    parser.add_argument("--expected-formal-count", type=int, default=4)
    parser.add_argument("--data-root", type=str, default=None)
    parser.add_argument("--json-report", type=str, default=None)
    args = parser.parse_args()
    audited_root = Path(args.data_root).absolute() if args.data_root else DEFAULT_DATA.absolute()
    report_path: Path | None = None
    if args.json_report:
        try:
            report_path = resolve_safe_report_path(
                args.json_report, audited_root=audited_root,
            )
        except ValueError as exc:
            print(f"ERROR: unsafe --json-report path: {exc}", file=sys.stderr)
            return 2
    report = audit_reset_state(
        data_root=audited_root, expected_formal_count=args.expected_formal_count,
    )
    json_text = json.dumps(report, ensure_ascii=False, indent=2)
    if report_path is not None:
        _write_report_atomically(
            report_path, json_text.encode("utf-8"), audited_root=audited_root,
        )
        print(f"Report written to {report_path}")
    else:
        print(json_text)
    if report["fresh_discovery_readiness"] == "BLOCKED_BY_ACTIVE_TRANSACTION":
        return 3
    if report["fresh_discovery_readiness"] in {"RESET_REQUIRED", "REPAIR_REQUIRED"}:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
