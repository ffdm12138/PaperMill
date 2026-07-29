"""Re-seal the active discovery generation's manifest (one-time operator tool).

After the final-freeze contract tightening, a generation sealed by the
pre-freeze lenient writer no longer resolves: its ``workspace.json`` has
empty set hashes and ``{}`` store schema versions, and may carry retired
fields.  This command recomputes the manifest from the actual workspace
content and rebinds the active pointer — the generation itself (notebooks,
page journals, exports) is untouched.

Default is a dry run: prints the planned new manifest and hashes without
writing.  ``--apply`` writes under the exclusive discovery maintenance
lock and verifies the resealed closure with a strict ``verify_tree``
resolve before exiting.
"""
from __future__ import annotations

import argparse
import json
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
from src.discovery.workspace import (  # noqa: E402
    reseal_active_generation_manifest,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Re-seal the active generation manifest under the "
        "strict final-freeze contract (dry run unless --apply).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the resealed manifest and rebind the active pointer",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.apply:
            with DiscoveryMaintenanceLock(purpose="reseal-active-manifest"):
                report = reseal_active_generation_manifest(apply=True)
        else:
            report = reseal_active_generation_manifest(apply=False)
    except DiscoveryMaintenanceLockError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # Corrupt pointer / damaged generation: fail closed, never guess.
        print(f"[ERROR] manifest reseal failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.apply:
        print(
            f"[OK] resealed active generation {report['generation_id']}: "
            f"manifest {report['old_manifest_sha256'][:16]}... -> "
            f"{report['new_manifest_sha256'][:16]}..."
        )
    else:
        print("[OK] dry run only — re-run with --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
