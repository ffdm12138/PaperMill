from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from src.catalog_folders.assignment import load_assignment, valid_decisions
from src.catalog_folders.formal_registry import FormalPaperRegistry
from src.catalog_folders.models import CLASSIFIER_SKILL_VERSION
from src.catalog_folders.reconcile import _categories
from src.file_fingerprint import compute_sha256
from src.utils.atomic_io import atomic_write_json


def canonical_hash(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def plan_tasks(*, root: Path, formal_registry: FormalPaperRegistry, paper_number: str | None = None, category_id: str | None = None, apply: bool) -> list[dict]:
    root = Path(root); state = root / ".state"
    categories = _categories(state / "category_registry.json")
    if category_id:
        categories = [category for category in categories if category.category_id == category_id]
        if not categories:
            raise ValueError(f"unknown active category: {category_id}")
    tasks: list[dict] = []
    for paper in formal_registry.load():
        if paper_number and paper.paper_number != paper_number:
            continue
        assignment = load_assignment(state / "assignments" / f"{paper.paper_number}.json")
        valid = valid_decisions(assignment, paper, categories)
        missing = [category for category in categories if category.category_id not in valid]
        if not missing:
            continue
        body = {
            "schema_version": "1.0", "paper_number": paper.paper_number, "paper_id": paper.paper_id,
            "catalog_path": str(paper.catalog_path.resolve()), "catalog_sha256": compute_sha256(paper.catalog_path),
            "classifier_skill_version": CLASSIFIER_SKILL_VERSION,
            "categories": [category.to_dict() for category in missing],
        }
        task_hash = canonical_hash(body)
        task = {**body, "task_id": str(uuid.uuid5(uuid.NAMESPACE_URL, task_hash)), "task_input_sha256": task_hash}
        tasks.append(task)
        if apply:
            path = state / "tasks" / paper.paper_number / f"{task['task_id']}.json"
            if path.exists() and json.loads(path.read_text(encoding="utf-8")) != task:
                raise RuntimeError(f"conflicting classification task: {path}")
            atomic_write_json(path, task, indent=2)
    if paper_number and not any(p.paper_number == paper_number for p in formal_registry.load()):
        raise ValueError(f"unknown paper_number: {paper_number}")
    return tasks
