"""Formal GPU wrapper for v2 paper_raw MinerU conversion."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--smoke-report", type=Path, default=None)
    parser.add_argument("--skip-smoke-check", action="store_true")
    wrapper_args, remaining = parser.parse_known_args(sys.argv[1:])

    if "--allow-cpu" in remaining:
        print(
            "ERROR: --allow-cpu is debug-only and is not allowed via "
            "scripts/convert_paper_raw_gpu.py.",
            file=sys.stderr,
        )
        return 2

    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))

    from src.mineru.runtime import activate_formal_gpu_env

    activate_formal_gpu_env()
    os.environ["MINERU_GPU_WRAPPER_ACTIVE"] = "1"

    print("Formal GPU MinerU conversion wrapper active.", file=sys.stderr)
    print(f"CUDA_VISIBLE_DEVICES={os.environ.get('CUDA_VISIBLE_DEVICES', '')}", file=sys.stderr)

    formal_batch_apply = "--all" in remaining and "--apply" in remaining and "--dry-run" not in remaining
    if formal_batch_apply and not wrapper_args.skip_smoke_check:
        from src.mineru.smoke import smoke_required_message, validate_smoke_report

        validation = validate_smoke_report(wrapper_args.smoke_report)
        if not validation.get("ok"):
            for error in validation.get("errors") or []:
                print(f"ERROR: {error}", file=sys.stderr)
            print(smoke_required_message(wrapper_args.smoke_report), file=sys.stderr)
            return 2

    from scripts.convert_paper_raw_batch import main as batch_main

    saved = sys.argv
    sys.argv = [saved[0], *remaining]
    try:
        return batch_main()
    finally:
        sys.argv = saved


if __name__ == "__main__":
    raise SystemExit(main())
