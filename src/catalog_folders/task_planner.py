from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from src.catalog_folders.assignment import load_assignment, valid_decisions
from src.catalog_folders.formal_registry import FormalPaperRegistry
from src.catalog_folders.models import CLASSIFIER_SKILL_VERSION, Category
from src.catalog_folders.registry import load_categories
from src.file_fingerprint import compute_sha256
from src.utils.atomic_io import atomic_write_json

CATALOG_CLASSIFICATION_MAX_CATEGORIES_PER_TASK = 20


def canonical_hash(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _task_id_from_body(body: dict, batch_ordinal: int) -> str:
    """Generate a deterministic task ID that includes the batch ordinal."""
    payload = {**body, "batch_ordinal": batch_ordinal}
    task_hash = canonical_hash(payload)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, task_hash))


def _build_task_body(
    paper: object,  # FormalPaper
    categories: list[Category],
    batch_ordinal: int,
    catalog_sha256: str,
) -> dict:
    return {
        "schema_version": "1.0",
        "paper_number": paper.paper_number,
        "paper_name": paper.paper_name,
        "catalog_path": str(paper.catalog_path.resolve()),
        "catalog_sha256": catalog_sha256,
        "classifier_skill_version": CLASSIFIER_SKILL_VERSION,
        "categories": [category.to_dict() for category in categories],
    }


def plan_tasks(
    *,
    root: Path,
    formal_registry: FormalPaperRegistry,
    paper_number: str | None = None,
    category_id: str | None = None,
    apply: bool,
    max_categories_per_task: int = CATALOG_CLASSIFICATION_MAX_CATEGORIES_PER_TASK,
) -> list[dict]:
    """Plan classification tasks for papers with missing decisions.

    When a paper has more missing categories than ``max_categories_per_task``,
    the categories are split across multiple batched tasks.  Each task carries
    a ``batch_ordinal`` in its identity so that a new category only generates
    tasks for that category, not for all others.

    New categories only produce tasks for the new category; existing tasks for
    other categories are left untouched.
    """
    root = Path(root); state = root / ".state"
    categories = load_categories(state / "category_registry.json")
    if category_id:
        categories = [category for category in categories if category.category_id == category_id]
        if not categories:
            raise ValueError(f"unknown active category: {category_id}")

    tasks: list[dict] = []
    for paper in formal_registry.load():
        if paper_number and paper.paper_number != paper_number:
            continue

        catalog_sha256 = compute_sha256(paper.catalog_path)
        assignment = load_assignment(state / "assignments" / f"{paper.paper_number}.json")
        valid = valid_decisions(assignment, paper, categories)
        missing = [category for category in categories if category.category_id not in valid]
        if not missing:
            continue

        # Split missing categories into batches
        for batch_idx in range(0, len(missing), max_categories_per_task):
            batch = missing[batch_idx:batch_idx + max_categories_per_task]
            batch_ordinal = batch_idx // max_categories_per_task
            body = _build_task_body(paper, batch, batch_ordinal, catalog_sha256)
            task_hash = canonical_hash({**body, "batch_ordinal": batch_ordinal})
            task_id = _task_id_from_body(body, batch_ordinal)
            task = {
                **body,
                "task_id": task_id,
                "task_input_sha256": task_hash,
                "batch_ordinal": batch_ordinal,
            }
            tasks.append(task)

            if apply:
                path = state / "tasks" / paper.paper_number / f"{task_id}.json"
                if path.exists():
                    existing_task = json.loads(path.read_text(encoding="utf-8"))
                    # Same task_id means same input → idempotent
                    if existing_task.get("task_input_sha256") != task_hash:
                        raise RuntimeError(
                            f"conflicting classification task at {path}: "
                            f"existing hash {existing_task.get('task_input_sha256','?')[:12]}… "
                            f"!= new hash {task_hash[:12]}…"
                        )
                else:
                    atomic_write_json(path, task, indent=2)

    if paper_number and not any(p.paper_number == paper_number for p in formal_registry.load()):
        raise ValueError(f"unknown paper_number: {paper_number}")
    return tasks
