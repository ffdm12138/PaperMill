from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from src.catalog_folders.formal_registry import FormalPaperRegistry
from src.catalog_folders.models import CLASSIFIER_SKILL_VERSION
from src.catalog_folders.registry import now_iso
from src.catalog_folders.task_planner import canonical_hash
from src.file_fingerprint import compute_sha256
from src.utils.atomic_io import atomic_write_json


SCHEMA_PATH = Path(__file__).resolve().parents[2] / "skills" / "catalog_folder_classifier" / "category_result_schema.json"


def apply_result(*, result_path: Path, root: Path, formal_registry: FormalPaperRegistry, apply: bool) -> dict:
    result_path = Path(result_path); root = Path(root); state = root / ".state"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft7Validator(schema).validate(result)
    number = result["paper_number"]; task_id = result["task_id"]
    task_path = state / "tasks" / number / f"{task_id}.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task_body = {key: value for key, value in task.items() if key not in {"task_id", "task_input_sha256"}}
    if task["task_input_sha256"] != canonical_hash(task_body) or result["task_input_sha256"] != task["task_input_sha256"]:
        raise ValueError("classification task hash mismatch")
    paper = formal_registry.resolve(number)
    if paper is None or paper.paper_id != task["paper_id"] or result["paper_id"] != task["paper_id"]:
        raise ValueError("classification formal identity mismatch")
    if compute_sha256(paper.catalog_path) != task["catalog_sha256"]:
        raise ValueError("classification Catalog hash is stale")
    categories = {row["category_id"]: row for row in task["categories"]}
    decisions = result["decisions"]
    if {row["category_id"] for row in decisions} != set(categories):
        raise ValueError("classification result category set mismatch")
    catalog = json.loads(paper.catalog_path.read_text(encoding="utf-8"))
    allowed_fields = set(catalog)
    merged: dict[str, dict] = {}
    classified_at = now_iso()
    for decision in decisions:
        unknown = set(decision["catalog_evidence_fields"]) - allowed_fields
        if unknown:
            raise ValueError(f"classification evidence fields absent from Catalog: {sorted(unknown)}")
        category = categories[decision["category_id"]]
        merged[decision["category_id"]] = {
            "category_definition_sha256": category["definition_sha256"],
            "matched": decision["matched"], "confidence": decision["confidence"],
            "reason_zh": decision["reason_zh"], "catalog_evidence_fields": decision["catalog_evidence_fields"],
            "classifier_skill_version": CLASSIFIER_SKILL_VERSION, "classified_at": classified_at,
        }
    assignment_path = state / "assignments" / f"{number}.json"
    existing = json.loads(assignment_path.read_text(encoding="utf-8")) if assignment_path.is_file() else {}
    old_decisions = existing.get("decisions") if isinstance(existing.get("decisions"), dict) else {}
    for category_id, decision in merged.items():
        old = old_decisions.get(category_id)
        if old and {k: old.get(k) for k in decision if k != "classified_at"} != {k: decision.get(k) for k in decision if k != "classified_at"}:
            raise RuntimeError(f"conflicting classification result: {category_id}")
        if old:
            merged[category_id] = old
    assignment = {
        "schema_version": "1.0", "paper_number": number, "paper_id": paper.paper_id,
        "catalog_sha256": task["catalog_sha256"], "updated_at": classified_at,
        "decisions": {**old_decisions, **merged},
    }
    receipt = state / "applied_results" / number / f"{task_id}.json"
    receipt_data = {"schema_version": "1.0", "task_id": task_id, "result_sha256": compute_sha256(result_path), "applied_at": classified_at}
    if receipt.is_file():
        prior = json.loads(receipt.read_text(encoding="utf-8"))
        if prior.get("result_sha256") != receipt_data["result_sha256"]:
            raise RuntimeError("conflicting replay for applied classification result")
        return assignment
    if apply:
        atomic_write_json(assignment_path, assignment, indent=2)
        atomic_write_json(receipt, receipt_data, indent=2)
    return assignment
