"""Show classification progress: how many tasks remain, what's complete.

Usage:
  python scripts/show_catalog_classification_progress.py
  python scripts/show_catalog_classification_progress.py --json
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from config.settings import CATALOG_FOLDER_ROOT, PAPERS_DIR, PAPER_NUMBER_LEDGER_PATH
from src.catalog_folders.formal_registry import FormalPaperRegistry
from src.catalog_folders.validation import doctor
from src.library.paper_number_ledger import PaperNumberLedger


def main(argv=None):
    p = argparse.ArgumentParser(description="Show catalog classification progress.")
    p.add_argument("--catalog-root", type=Path, default=CATALOG_FOLDER_ROOT)
    p.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    p.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    reg = FormalPaperRegistry(papers_dir=a.papers_dir, ledger=PaperNumberLedger(a.ledger_path))
    d = doctor(root=a.catalog_root, formal_registry=reg)

    # Count pending and applied tasks
    tasks_dir = a.catalog_root / ".state" / "tasks"
    total_tasks = 0
    applied_tasks = 0
    if tasks_dir.is_dir():
        for task_file in tasks_dir.rglob("*.json"):
            total_tasks += 1
            paper_number = task_file.parent.name
            task_id = task_file.stem
            receipt = a.catalog_root / ".state" / "applied_results" / paper_number / f"{task_id}.json"
            if receipt.is_file():
                applied_tasks += 1

    pending_tasks = total_tasks - applied_tasks
    required = d["active_formal_papers"] * d["categories"]
    completion_pct = 0
    if required > 0:
        valid_decisions = required - d["missing_decisions"]
        completion_pct = round(valid_decisions / required * 100, 1)

    progress = {
        "formal_papers": d["active_formal_papers"],
        "categories": d["categories"],
        "required_decisions": required,
        "valid_decisions": required - d["missing_decisions"],
        "missing_decisions": d["missing_decisions"],
        "stale_decisions": d["stale_decisions"],
        "pending_tasks": pending_tasks,
        "applied_tasks": applied_tasks,
        "total_tasks": total_tasks,
        "pending_papers": d["pending"],
        "completion_pct": completion_pct,
        "writer_category_safe": d["writer_category_safe"],
        "classification_complete": d["classification_complete"],
    }

    if a.json:
        print(json.dumps(progress, ensure_ascii=False, indent=2))
    else:
        print(f"Formal papers:              {progress['formal_papers']}")
        print(f"Categories:                 {progress['categories']}")
        print(f"Required decisions:         {progress['required_decisions']}")
        print(f"Valid decisions:            {progress['valid_decisions']}")
        print(f"Missing decisions:          {progress['missing_decisions']}")
        print(f"Stale decisions:            {progress['stale_decisions']}")
        print(f"Pending tasks:              {progress['pending_tasks']}")
        print(f"Applied tasks:              {progress['applied_tasks']}")
        print(f"Pending papers:             {progress['pending_papers']}")
        print(f"Completion:                 {progress['completion_pct']}%")
        print(f"Classification complete:    {progress['classification_complete']}")
        print(f"Writer category safe:       {progress['writer_category_safe']}")

    return 0 if progress["classification_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
