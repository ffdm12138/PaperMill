"""Start or reuse the local persistent mineru-api service."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from config.settings import CUDA_PATH_DEFAULT
from src.mineru_service_manager import dumps_result, start_services


def main() -> int:
    parser = argparse.ArgumentParser(description="Start/reuse local mineru-api service.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--api-url", default=None)
    parser.add_argument("--cuda-visible-devices", default="0")
    parser.add_argument("--cuda-path", default=CUDA_PATH_DEFAULT)
    parser.add_argument("--no-vlm-preload", action="store_true")
    parser.add_argument("--wait", action="store_true", help="wait for /health to become ready")
    parser.add_argument("--wait-seconds", type=float, default=60.0)
    parser.add_argument("--restart-if-stale", action="store_true",
                        help="restart a pid-file-managed mineru-api when /health is healthy but identity is stale")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--web", action="store_true", help="also start python -m src.server")
    args = parser.parse_args()

    result = start_services(
        port=args.port,
        api_url=args.api_url,
        cuda_visible_devices=args.cuda_visible_devices,
        cuda_path=args.cuda_path,
        vlm_preload=not args.no_vlm_preload,
        wait=args.wait,
        wait_seconds=args.wait_seconds,
        restart_if_stale=args.restart_if_stale,
        web=args.web,
    )
    if args.json:
        print(dumps_result(result))
    else:
        status = "OK" if result.get("ok") else "FAILED"
        print(f"[{status}] {result.get('message', '')}")
        print(f"mineru-api: {result.get('mineru_api_url', '')}")
        print(f"pid file: {result.get('pid_file', '')}")
        print(f"log file: {result.get('log_file', '')}")
        if result.get("next_command"):
            print(f"next: {result['next_command']}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
