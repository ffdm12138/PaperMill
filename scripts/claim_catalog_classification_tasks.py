"""Claim pending classification tasks for a worker.

Lists the next unapplied tasks with their Catalog paths so an agent (Codex)
can read each Catalog and produce real classification results.

Usage:
  python scripts/claim_catalog_classification_tasks.py --worker codex --max-tasks 5

Outputs JSON array of tasks, each with:
  - task_path: absolute path to the task JSON
  - catalog_path: absolute path to the Catalog to read
  - paper_number, paper_name, task_id, category count
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from config.settings import CATALOG_FOLDER_ROOT, PAPERS_DIR, PAPER_NUMBER_LEDGER_PATH


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Claim pending classification tasks for a worker."
    )
    p.add_argument("--catalog-root", type=Path, default=CATALOG_FOLDER_ROOT)
    p.add_argument("--worker", type=str, default="codex",
                   help="Worker identifier (default: codex)")
    p.add_argument("--max-tasks", type=int, default=5,
                   help="Maximum number of tasks to claim")
    p.add_argument("--json", action="store_true", default=True,
                   help="Output as JSON (default)")
    a = p.parse_args(argv)

    tasks_dir = a.catalog_root / ".state" / "tasks"
    if not tasks_dir.is_dir():
        print(json.dumps({"tasks": [], "claimed": 0, "message": "no tasks directory"}, ensure_ascii=False))
        return 0

    claimed: list[dict] = []
    for paper_dir in sorted(tasks_dir.iterdir()):
        if not paper_dir.is_dir():
            continue
        if len(claimed) >= a.max_tasks:
            break
        paper_number = paper_dir.name
        for task_file in sorted(paper_dir.glob("*.json")):
            if len(claimed) >= a.max_tasks:
                break
            task_id = task_file.stem
            receipt_path = a.catalog_root / ".state" / "applied_results" / paper_number / f"{task_id}.json"
            if receipt_path.is_file():
                continue
            try:
                task = json.loads(task_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            claimed.append({
                "task_id": task_id,
                "task_path": str(task_file.resolve()),
                "paper_number": paper_number,
                "paper_name": task.get("paper_name", ""),
                "catalog_path": task.get("catalog_path", ""),
                "category_count": len(task.get("categories", [])),
                "category_ids": [c["category_id"] for c in task.get("categories", [])],
                "category_keywords": [c.get("keyword_zh", "?") for c in task.get("categories", [])],
            })

    result = {
        "worker": a.worker,
        "claimed": len(claimed),
        "tasks": claimed,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if len(claimed) == 0:
        print("\nNo pending tasks. All classification complete.", file=sys.stderr)
    else:
        print(f"\nClaimed {len(claimed)} tasks for worker '{a.worker}'.", file=sys.stderr)
        print("For each task:", file=sys.stderr)
        print("  1. Read the Catalog at catalog_path", file=sys.stderr)
        print("  2. For each category, decide matched=true/false", file=sys.stderr)
        print("  3. Write result to data/catalog/.state/results/<paper_number>/<task_id>.json", file=sys.stderr)
        print(f"  4. Apply: python scripts/apply_catalog_classification_result.py --result <path> --apply", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
