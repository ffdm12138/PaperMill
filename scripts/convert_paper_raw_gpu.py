"""Formal GPU wrapper for v2 paper_raw MinerU conversion."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    if "--allow-cpu" in sys.argv[1:]:
        print(
            "ERROR: --allow-cpu is debug-only and is not allowed via "
            "scripts/convert_paper_raw_gpu.py.",
            file=sys.stderr,
        )
        return 2

    os.environ["MINERU_REQUIRE_GPU"] = "true"
    os.environ.pop("MINERU_ALLOW_CPU", None)
    os.environ["CUDA_VISIBLE_DEVICES"] = "0"
    os.environ["MINERU_BACKEND"] = "hybrid-engine"
    os.environ["MINERU_METHOD"] = "auto"
    os.environ["MINERU_EFFORT"] = "medium"
    os.environ.setdefault("MINERU_RUNNER", "cli_api_proxy")
    os.environ.setdefault("MINERU_API_URL", "http://127.0.0.1:8000")
    os.environ["MINERU_GPU_WRAPPER_ACTIVE"] = "1"

    print("Formal GPU MinerU conversion wrapper active.", file=sys.stderr)
    print(f"CUDA_VISIBLE_DEVICES={os.environ.get('CUDA_VISIBLE_DEVICES', '')}", file=sys.stderr)

    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))
    from scripts.convert_paper_raw_batch import main as batch_main

    return batch_main()


if __name__ == "__main__":
    raise SystemExit(main())
