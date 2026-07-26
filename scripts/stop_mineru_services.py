"""Stop local mineru-api services started for conversion."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.mineru.service_manager import dumps_result, stop_services
from src.mineru.lock import clear_stale_mineru_lock, read_mineru_lock_status


def main() -> int:
    parser = argparse.ArgumentParser(description="Stop local mineru-api service.")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--all-mineru-api", action="store_true",
                        help="stop all processes whose command line contains mineru-api")
    parser.add_argument("--stale-locks", action="store_true",
                        help="remove only MinerU conversion locks whose owner PID is no longer alive")
    parser.add_argument("--web", action="store_true", help="also stop project web server if web pid file exists")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = stop_services(
        force=args.force,
        all_mineru_api=args.all_mineru_api,
        web=args.web,
    )
    if args.stale_locks:
        before = read_mineru_lock_status()
        cleared = clear_stale_mineru_lock()
        result["stale_lock"] = {
            "before": before,
            "cleared": cleared,
            "after": read_mineru_lock_status(),
        }
    if args.json:
        print(dumps_result(result))
    else:
        status = "OK" if result.get("ok") else "FAILED"
        print(f"[{status}] {result.get('message', '')}")
        if result.get("stopped_pids"):
            print("stopped: " + ", ".join(str(p) for p in result["stopped_pids"]))
        if args.stale_locks:
            stale = result.get("stale_lock") or {}
            print(f"stale_lock_cleared: {stale.get('cleared')}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
