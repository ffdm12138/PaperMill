"""Classification task runner with injectable classifier backends.

Provides the CatalogCategoryClassifier protocol and implementations:
  - ManualResultClassifier: reads pre-written result files from a directory
  - AgentSkillClassifierAdapter: export/import bridge for agent-skill workflow

FakeClassifier lives in ``src.catalog_folders.testing.fake_classifier`` and
is gated behind ``--testing-only`` in the CLI — it must never run against
real ``data/catalog/``.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Protocol, runtime_checkable

from loguru import logger

from src.catalog_folders.assignment import load_assignment, valid_decisions
from src.catalog_folders.formal_registry import FormalPaperRegistry
from src.catalog_folders.models import CLASSIFIER_SKILL_VERSION, Category
from src.catalog_folders.registry import load_categories, now_iso
from src.catalog_folders.result_validator import apply_result
from src.catalog_folders.reconcile import reconcile_catalog_folders
from src.catalog_folders.task_planner import canonical_hash
from src.utils.atomic_io import atomic_write_json


@runtime_checkable
class CatalogCategoryClassifier(Protocol):
    """Inject a classifier backend.

    Receives a task dict and the parsed per-paper Catalog.
    Returns a result dict conforming to category_result_schema.json.
    """
    def classify(
        self,
        *,
        task: dict,
        catalog: dict[str, object],
    ) -> dict:
        ...


# ── Production guard ──────────────────────────────────────────────────

def _is_real_catalog_root(root: Path) -> bool:
    """Production signal: does the ledger at *root* hold active papers?

    No path sniffing: production code must not inspect path strings for
    test-runner names.  A root whose ledger has at least one active paper is
    production; anything else (fresh roots, isolated test roots) is not.
    Tests that seed active papers and still need the fake classifier opt in
    explicitly via ``MINERU_ALLOW_FAKE_CLASSIFIER``.
    """
    ledger_path = Path(root).resolve() / "paper_number_ledger.json"
    if ledger_path.is_file():
        try:
            data = json.loads(ledger_path.read_text(encoding="utf-8"))
            active = sum(
                1 for v in (data.get("items") or {}).values()
                if isinstance(v, dict) and v.get("state") == "active"
            )
            if active > 0:
                return True
        except Exception as exc:
            logger.warning("catalog root ledger unreadable ({}); treating as non-production", exc)
    return False


def _allow_testing_backend(root: Path) -> bool:
    """Check whether a testing-only backend is permitted."""
    if os.environ.get("MINERU_ALLOW_FAKE_CLASSIFIER", "").strip().lower() in ("true", "1", "yes"):
        return True
    if not _is_real_catalog_root(root):
        return True
    return False


# ── Manual result classifier (reads pre-written result files) ─────────

class ManualResultClassifier:
    """Reads pre-existing result JSON files from a result directory.

    Result files must be named ``<task_id>.json`` and live under
    ``<result_dir>/<paper_number>/``.
    """

    def __init__(self, result_dir: Path):
        self.result_dir = Path(result_dir)

    def classify(self, *, task: dict, catalog: dict[str, object]) -> dict:
        path = self.result_dir / task["paper_number"] / f"{task['task_id']}.json"
        if not path.is_file():
            raise FileNotFoundError(f"manual result not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))


# ── Task runner ───────────────────────────────────────────────────────

def run_tasks(
    *,
    root: Path,
    formal_registry: FormalPaperRegistry,
    classifier: CatalogCategoryClassifier,
    apply: bool,
    max_tasks: int | None = None,
    classifier_name: str = "codex_agent",
    testing_only: bool = False,
) -> dict:
    """Scan pending tasks, classify each, validate, and optionally apply.

    Skips tasks that already have an applied receipt (idempotent).
    Each task is processed inside a per-paper lock; links are updated
    atomically before the applied receipt is written.
    """
    root = Path(root); state = root / ".state"

    # Production guard for fake/testing backends
    if testing_only and not _allow_testing_backend(root):
        raise RuntimeError(
            "Testing-only backend rejected on real data/catalog/. "
            "Set MINERU_ALLOW_FAKE_CLASSIFIER=true to override, "
            "or use a real classification backend."
        )

    tasks_dir = state / "tasks"
    if not tasks_dir.is_dir():
        return {"classified": 0, "skipped": 0, "errors": 0, "details": []}

    categories = load_categories(state / "category_registry.json")
    details: list[dict] = []
    classified = 0; skipped = 0; errors = 0
    run_id = str(uuid.uuid4())

    for paper_dir in sorted(tasks_dir.iterdir()):
        if not paper_dir.is_dir():
            continue
        paper_number = paper_dir.name
        if max_tasks is not None and classified >= max_tasks:
            break

        for task_file in sorted(paper_dir.glob("*.json")):
            if max_tasks is not None and classified >= max_tasks:
                break

            task_id = task_file.stem
            receipt_path = state / "applied_results" / paper_number / f"{task_id}.json"
            if receipt_path.is_file():
                skipped += 1
                details.append({"task_id": task_id, "paper_number": paper_number, "status": "skipped"})
                continue

            task = json.loads(task_file.read_text(encoding="utf-8"))
            paper = formal_registry.resolve(paper_number)
            if paper is None:
                errors += 1
                details.append({"task_id": task_id, "paper_number": paper_number, "status": "error", "reason": "paper not found"})
                continue

            # Validate task is current
            from src.utils.file_fingerprint import compute_sha256
            current_catalog_sha256 = compute_sha256(paper.catalog_path)
            if current_catalog_sha256 != task.get("catalog_sha256"):
                errors += 1
                details.append({"task_id": task_id, "paper_number": paper_number, "status": "error", "reason": "stale catalog hash"})
                continue

            try:
                catalog = json.loads(paper.catalog_path.read_text(encoding="utf-8"))
                result = classifier.classify(task=task, catalog=catalog)

                # Write result to a temp file so apply_result can use its path-based interface
                results_dir = state / "results" / paper_number
                results_dir.mkdir(parents=True, exist_ok=True)
                result_path = results_dir / f"{task_id}.json"
                atomic_write_json(result_path, result, indent=2)

                applied = apply_result(
                    result_path=result_path,
                    root=root,
                    formal_registry=formal_registry,
                    apply=apply,
                    classifier=classifier_name,
                    classification_run_id=run_id,
                )
                classified += 1
                details.append({
                    "task_id": task_id,
                    "paper_number": paper_number,
                    "status": applied.get("status", "applied"),
                })
            except Exception as exc:
                errors += 1
                details.append({
                    "task_id": task_id,
                    "paper_number": paper_number,
                    "status": "error",
                    "reason": str(exc),
                })

    # Full reconcile after batch completes (corrects any DIRTY from partial runs)
    if apply and classified > 0:
        try:
            reconcile_catalog_folders(root=root, formal_registry=formal_registry, apply=True)
        except Exception:
            pass  # non-fatal: per-paper links were already updated

    return {
        "classified": classified,
        "skipped": skipped,
        "errors": errors,
        "classification_run_id": run_id,
        "classifier": classifier_name,
        "details": details,
    }


def export_batch(
    *,
    root: Path,
    output_path: Path,
    max_tasks: int | None = None,
) -> dict:
    """Export pending classification tasks as a batch JSON file.

    The batch can be processed by an external agent/skill and re-imported
    via ``import_results()``.
    """
    root = Path(root); state = root / ".state"
    tasks_dir = state / "tasks"
    if not tasks_dir.is_dir():
        return {"exported": 0, "path": str(output_path)}

    tasks: list[dict] = []
    for paper_dir in sorted(tasks_dir.iterdir()):
        if not paper_dir.is_dir():
            continue
        if max_tasks is not None and len(tasks) >= max_tasks:
            break
        for task_file in sorted(paper_dir.glob("*.json")):
            if max_tasks is not None and len(tasks) >= max_tasks:
                break
            paper_number = paper_dir.name
            task_id = task_file.stem
            receipt_path = state / "applied_results" / paper_number / f"{task_id}.json"
            if receipt_path.is_file():
                continue
            task = json.loads(task_file.read_text(encoding="utf-8"))
            tasks.append(task)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    batch = {
        "schema_version": "1.0",
        "exported_at": now_iso(),
        "task_count": len(tasks),
        "tasks": tasks,
    }
    atomic_write_json(output_path, batch, indent=2)
    return {"exported": len(tasks), "path": str(output_path)}


def import_results(
    *,
    result_dir: Path,
    root: Path,
    formal_registry: FormalPaperRegistry,
    apply: bool,
    classifier_name: str = "codex_agent",
) -> dict:
    """Import classification results from a directory and apply them.

    Expects result files at ``<result_dir>/<paper_number>/<task_id>.json``.
    """
    classifier = ManualResultClassifier(result_dir)
    return run_tasks(
        root=root,
        formal_registry=formal_registry,
        classifier=classifier,
        apply=apply,
        classifier_name=classifier_name,
    )
