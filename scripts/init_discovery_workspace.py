"""Initialize the first discovery v4 workspace on a fresh install.

A runtime-zero deployment has no ``data/discovery/active_generation.json``,
so every production discovery tool fails closed until one generation exists.
This command is the formal bootstrap entry point: it creates a strict,
completely empty v4 generation (all five subdirectories plus
``workspace.json``), writes the active pointer, and exits.  It imports no
legacy data and enables no keyword notebooks — notebooks are added
afterwards through ``manage_discovery_keywords.py``.

Idempotent: when a valid active generation already resolves, the command
reports it and exits 0 without touching anything.  A corrupt existing
closure fails closed instead of being overwritten.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from src.discovery.maintenance_gate import (  # noqa: E402
    DiscoveryMaintenanceLock,
    DiscoveryMaintenanceLockError,
)
from src.discovery.workspace import bootstrap_initial_workspace  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create and activate the first discovery v4 workspace.",
    )
    parser.add_argument(
        "--generation-id",
        default=None,
        help="explicit generation id (default: v4-<random>)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        with DiscoveryMaintenanceLock(purpose="bootstrap-v4-init"):
            workspace, created = bootstrap_initial_workspace(
                generation_id=args.generation_id,
            )
    except DiscoveryMaintenanceLockError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Corrupt pointer / damaged manifest / cutover conflict: fail closed.
        print(f"[ERROR] discovery workspace bootstrap failed: {exc}", file=sys.stderr)
        return 1

    if created:
        print(f"[OK] initialized discovery v4 workspace: {workspace.root}")
        print("[OK] active generation written; no keyword notebooks enabled")
    else:
        print(f"[OK] active discovery v4 workspace already present: {workspace.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
