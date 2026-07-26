from __future__ import annotations

import json
from pathlib import Path

from src.catalog_folders.models import CLASSIFIER_SKILL_VERSION, Category
from src.catalog_folders.formal_registry import FormalPaper
from src.utils.file_fingerprint import compute_sha256


def load_assignment(path: Path) -> dict | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def valid_decisions(assignment: dict | None, paper: FormalPaper, categories: list[Category]) -> dict[str, dict]:
    if not assignment or assignment.get("schema_version") != "1.0":
        return {}
    if assignment.get("paper_number") != paper.paper_number or assignment.get("paper_name") != paper.paper_name:
        return {}
    if assignment.get("catalog_sha256") != compute_sha256(paper.catalog_path):
        return {}
    decisions = assignment.get("decisions") if isinstance(assignment.get("decisions"), dict) else {}
    valid: dict[str, dict] = {}
    for category in categories:
        decision = decisions.get(category.category_id)
        if not isinstance(decision, dict):
            continue
        if decision.get("category_definition_sha256") != category.definition_sha256:
            continue
        if decision.get("classifier_skill_version") != CLASSIFIER_SKILL_VERSION:
            continue
        if type(decision.get("matched")) is not bool:
            continue
        valid[category.category_id] = decision
    return valid
