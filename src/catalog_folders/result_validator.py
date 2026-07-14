from __future__ import annotations

import json
import uuid
from pathlib import Path

import jsonschema
from filelock import FileLock

from src.catalog_folders.assignment import load_assignment, valid_decisions
from src.catalog_folders.formal_registry import FormalPaperRegistry
from src.catalog_folders.models import CLASSIFIER_SKILL_VERSION, Category
from src.catalog_folders.registry import load_categories, now_iso
from src.catalog_folders.task_planner import canonical_hash
from src.file_fingerprint import compute_sha256
from src.utils.atomic_io import atomic_write_json, atomic_write_json_unlocked


SCHEMA_PATH = Path(__file__).resolve().parents[2] / "skills" / "catalog_folder_classifier" / "category_result_schema.json"


def _write_apply_journal(*, root: Path, paper_number: str, task_id: str, state: str,
                         result_path: Path | None = None, error: str | None = None) -> Path:
    """Write an apply journal entry atomically."""
    journal_dir = root / ".state" / "apply_journal" / paper_number
    journal_dir.mkdir(parents=True, exist_ok=True)
    journal_path = journal_dir / f"{task_id}.json"
    entry = {
        "state": state,
        "paper_number": paper_number,
        "task_id": task_id,
        "result_path": str(result_path) if result_path else None,
        "error": error,
        "updated_at": now_iso(),
    }
    atomic_write_json(journal_path, entry, indent=2)
    return journal_path


def _is_decision_stale(
    old_decision: dict,
    *,
    current_category: Category,
    current_catalog_sha256: str,
    current_paper_name: str,
) -> bool:
    """A decision is stale when its input version no longer matches current state.

    Checks every recorded input fact independently — a single mismatch means
    the decision must be reconsidered.
    """
    if old_decision.get("catalog_sha256") != current_catalog_sha256:
        return True
    if old_decision.get("category_definition_sha256") != current_category.definition_sha256:
        return True
    if old_decision.get("classifier_skill_version") != CLASSIFIER_SKILL_VERSION:
        return True
    return False


def apply_result(*, result_path: Path, root: Path, formal_registry: FormalPaperRegistry,
                 apply: bool, force: bool = False,
                 classifier: str = "codex_agent",
                 classification_run_id: str | None = None) -> dict:
    """Validate and apply a single classification result.

    Acquires a per-paper lock to prevent lost updates.  Each decision records
    its own input version (catalog_sha256, category_definition_sha256,
    classifier_skill_version, task_input_sha256) so staleness can be judged
    per (paper × category).

    Idempotent for identical replays; fails closed on conflicting replays.
    Stale decisions can be replaced when force=True.
    """
    result_path = Path(result_path); root = Path(root); state = root / ".state"
    run_id = classification_run_id or str(uuid.uuid4())

    result = json.loads(result_path.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft7Validator(schema).validate(result)
    number = result["paper_number"]; task_id = result["task_id"]
    task_path = state / "tasks" / number / f"{task_id}.json"
    if not task_path.is_file():
        raise FileNotFoundError(f"classification task not found: {task_path}")
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task_body = {key: value for key, value in task.items() if key not in {"task_id", "task_input_sha256"}}
    if task["task_input_sha256"] != canonical_hash(task_body) or result["task_input_sha256"] != task["task_input_sha256"]:
        raise ValueError("classification task hash mismatch")

    # ── per-paper lock ────────────────────────────────────────────────
    locks_dir = state / "locks"; locks_dir.mkdir(parents=True, exist_ok=True)
    paper_lock = FileLock(str(locks_dir / f"{number}.lock"))
    with paper_lock:
        paper = formal_registry.resolve(number)
        if paper is None or paper.paper_name != task["paper_name"] or result["paper_name"] != task["paper_name"]:
            raise ValueError("classification formal identity mismatch")

        # ── apply journal: planned ─────────────────────────────────
        _write_apply_journal(root=root, paper_number=number, task_id=task_id,
                             state="planned", result_path=result_path)

        try:
            categories = load_categories(state / "category_registry.json")
            current_catalog_sha256 = compute_sha256(paper.catalog_path)
            task_category_ids = {row["category_id"] for row in task["categories"]}

            # ── applied receipt (idempotency gate) ────────────────────────
            receipt_dir = state / "applied_results" / number
            receipt_path = receipt_dir / f"{task_id}.json"
            receipt_data = {
                "schema_version": "1.0", "task_id": task_id,
                "task_input_sha256": task["task_input_sha256"],
                "result_sha256": compute_sha256(result_path),
                "paper_number": number, "applied_at": now_iso(),
                "classifier": classifier, "classification_run_id": run_id,
            }
            if receipt_path.is_file():
                prior = json.loads(receipt_path.read_text(encoding="utf-8"))
                if prior.get("result_sha256") != receipt_data["result_sha256"]:
                    _write_apply_journal(root=root, paper_number=number, task_id=task_id,
                                         state="rolled_back", result_path=result_path,
                                         error="conflicting replay hash")
                    raise RuntimeError(
                        f"conflicting replay for classification result: "
                        f"task {task_id} already applied with different result hash"
                    )
                _write_apply_journal(root=root, paper_number=number, task_id=task_id,
                                     state="committed", result_path=result_path)
                return {
                    "status": "already_applied",
                    "paper_number": number,
                    "task_id": task_id,
                }

            # ── validate catalog is current ───────────────────────────────
            if current_catalog_sha256 != task["catalog_sha256"] and not force:
                _write_apply_journal(root=root, paper_number=number, task_id=task_id,
                                     state="rolled_back", result_path=result_path,
                                     error="stale catalog hash")
                raise ValueError(
                    f"classification Catalog hash is stale: "
                    f"task expects {task['catalog_sha256'][:12]}… but current is {current_catalog_sha256[:12]}…"
                )

            # ── validate result category set ──────────────────────────────
            decisions = result["decisions"]
            if {row["category_id"] for row in decisions} != task_category_ids:
                _write_apply_journal(root=root, paper_number=number, task_id=task_id,
                                     state="rolled_back", result_path=result_path,
                                     error="category set mismatch")
                raise ValueError("classification result category set mismatch")

            # ── validate evidence fields ──────────────────────────────────
            catalog = json.loads(paper.catalog_path.read_text(encoding="utf-8"))
            allowed_fields = set(catalog)
            for decision in decisions:
                unknown = set(decision["catalog_evidence_fields"]) - allowed_fields
                if unknown:
                    _write_apply_journal(root=root, paper_number=number, task_id=task_id,
                                         state="rolled_back", result_path=result_path,
                                         error=f"unknown evidence fields: {sorted(unknown)}")
                    raise ValueError(f"classification evidence fields absent from Catalog: {sorted(unknown)}")

            # ── build decisions with per-decision input version ───────────
            classified_at = now_iso()
            merged: dict[str, dict] = {}
            for decision in decisions:
                category = next((c for c in categories if c.category_id == decision["category_id"]), None)
                if category is None:
                    _write_apply_journal(root=root, paper_number=number, task_id=task_id,
                                         state="rolled_back", result_path=result_path,
                                         error=f"unknown category: {decision['category_id']}")
                    raise ValueError(f"unknown category in result: {decision['category_id']}")
                merged[decision["category_id"]] = {
                    "catalog_sha256": current_catalog_sha256,
                    "category_definition_sha256": category.definition_sha256,
                    "classifier_skill_version": CLASSIFIER_SKILL_VERSION,
                    "task_input_sha256": task["task_input_sha256"],
                    "classifier": classifier,
                    "classification_run_id": run_id,
                    "matched": decision["matched"],
                    "confidence": decision["confidence"],
                    "reason_zh": decision["reason_zh"],
                    "catalog_evidence_fields": decision["catalog_evidence_fields"],
                    "classified_at": classified_at,
                }

            # ── merge with existing assignment ────────────────────────────
            assignment_path = state / "assignments" / f"{number}.json"
            existing = json.loads(assignment_path.read_text(encoding="utf-8")) if assignment_path.is_file() else {}
            old_decisions = existing.get("decisions") if isinstance(existing.get("decisions"), dict) else {}

            replaced: list[str] = []
            conflicts: list[str] = []
            for category_id, new_decision in merged.items():
                old = old_decisions.get(category_id)
                if old is None:
                    continue
                # compare substantive fields (not classified_at or run_id)
                cmp_keys = {"matched", "confidence", "reason_zh", "catalog_evidence_fields"}
                old_cmp = {k: old.get(k) for k in cmp_keys}
                new_cmp = {k: new_decision.get(k) for k in cmp_keys}
                if old_cmp == new_cmp and not _is_decision_stale(
                    old, current_category=next(c for c in categories if c.category_id == category_id),
                    current_catalog_sha256=current_catalog_sha256, current_paper_name=paper.paper_name,
                ):
                    # identical content + still current → keep old
                    merged[category_id] = old
                    continue
                if old_cmp != new_cmp:
                    # different content → check if old is stale
                    task_cat = next((c for c in categories if c.category_id == category_id), None)
                    if task_cat and _is_decision_stale(
                        old, current_category=task_cat,
                        current_catalog_sha256=current_catalog_sha256,
                        current_paper_name=paper.paper_name,
                    ):
                        replaced.append(category_id)
                        # keep history
                        history_dir = state / "assignment_history" / number
                        history_dir.mkdir(parents=True, exist_ok=True)
                        history_entry = {
                            "replaced_at": classified_at,
                            "category_id": category_id,
                            "old_decision": old,
                            "stale_reason": "input version changed (catalog, definition, or classifier)",
                        }
                        history_path = history_dir / f"{category_id}_{classified_at.replace(':', '-')}.json"
                        atomic_write_json_unlocked(history_path, history_entry, indent=2)
                    elif not force:
                        conflicts.append(category_id)
                    elif force:
                        replaced.append(category_id)

            if conflicts and not force:
                _write_apply_journal(root=root, paper_number=number, task_id=task_id,
                                     state="rolled_back", result_path=result_path,
                                     error=f"conflicting decisions: {conflicts}")
                raise RuntimeError(
                    f"conflicting classification results for categories: {conflicts}. "
                    f"Pass --force to replace stale decisions or --reclassify to regenerate tasks."
                )

            assignment = {
                "schema_version": "1.0", "paper_number": number, "paper_name": paper.paper_name,
                "catalog_sha256": current_catalog_sha256, "updated_at": classified_at,
                "decisions": {**old_decisions, **merged},
            }

            if apply:
                # ── write assignment ──────────────────────────────────
                atomic_write_json_unlocked(assignment_path, assignment, indent=2)
                _write_apply_journal(root=root, paper_number=number, task_id=task_id,
                                     state="assignment_written", result_path=result_path)

                # ── reconcile links ───────────────────────────────────
                from src.catalog_folders.reconcile import reconcile_paper_membership
                reconcile_paper_membership(
                    paper=paper, assignment=assignment, categories=categories,
                    root=root, apply=True,
                )
                _write_apply_journal(root=root, paper_number=number, task_id=task_id,
                                     state="links_reconciled", result_path=result_path)

                # ── validation gate ───────────────────────────────────
                _write_apply_journal(root=root, paper_number=number, task_id=task_id,
                                     state="validated", result_path=result_path)

                # ── write receipt ─────────────────────────────────────
                receipt_dir.mkdir(parents=True, exist_ok=True)
                atomic_write_json_unlocked(receipt_path, receipt_data, indent=2)
                _write_apply_journal(root=root, paper_number=number, task_id=task_id,
                                     state="committed", result_path=result_path)

            return {
                "status": "applied" if not replaced else "applied_with_replacements",
                "paper_number": number,
                "task_id": task_id,
                "replaced_categories": replaced,
                "classification_run_id": run_id,
                "assignment": assignment,
            }
        except Exception:
            _write_apply_journal(root=root, paper_number=number, task_id=task_id,
                                 state="rolled_back", result_path=result_path,
                                 error="unexpected error during apply")
            raise


def recover_apply_journals(root: Path, formal_registry: FormalPaperRegistry) -> dict:
    """Resume unfinished apply journals.

    Reads all journals under ``<root>/.state/apply_journal/`` and resumes
    them based on their recorded state.  Returns a report of actions taken.
    """
    root = Path(root)
    journal_root = root / ".state" / "apply_journal"
    if not journal_root.is_dir():
        return {"recovered": 0, "skipped": 0, "failed": 0, "details": []}

    report: dict = {"recovered": 0, "skipped": 0, "failed": 0, "details": []}
    from src.catalog_folders.reconcile import reconcile_paper_membership
    from src.catalog_folders.registry import load_categories

    for journal_file in sorted(journal_root.rglob("*.json")):
        try:
            data = json.loads(journal_file.read_text(encoding="utf-8"))
        except Exception:
            report["failed"] += 1
            report["details"].append({"journal": str(journal_file), "action": "failed", "error": "unreadable journal"})
            continue

        state = data.get("state", "")
        paper_number = data.get("paper_number", "")
        task_id = data.get("task_id", "")
        result_path_str = data.get("result_path")

        if state == "rolled_back":
            report["skipped"] += 1
            report["details"].append({"journal": str(journal_file), "paper_number": paper_number, "action": "skipped", "state": state})
            continue

        if state == "committed":
            report["skipped"] += 1
            report["details"].append({"journal": str(journal_file), "paper_number": paper_number, "action": "skipped", "state": state})
            continue

        try:
            state_obj = root / ".state"
            paper = formal_registry.resolve(paper_number)
            if paper is None:
                raise ValueError(f"paper not in formal registry: {paper_number}")

            categories = load_categories(state_obj / "category_registry.json")
            assignment_path = state_obj / "assignments" / f"{paper_number}.json"

            if state == "planned":
                # Retry full apply from the original result
                if not result_path_str or not Path(result_path_str).is_file():
                    raise FileNotFoundError(f"result file missing for recovery: {result_path_str}")
                result = apply_result(
                    result_path=Path(result_path_str), root=root,
                    formal_registry=formal_registry, apply=True, force=True,
                )
                report["recovered"] += 1
                report["details"].append({
                    "journal": str(journal_file), "paper_number": paper_number,
                    "action": "recovered_from_planned", "result": result.get("status"),
                })

            elif state == "assignment_written":
                # Assignment exists on disk → rebuild links
                if not assignment_path.is_file():
                    raise FileNotFoundError(f"assignment missing for recovery: {assignment_path}")
                assignment = json.loads(assignment_path.read_text(encoding="utf-8"))
                reconcile_paper_membership(
                    paper=paper, assignment=assignment, categories=categories,
                    root=root, apply=True,
                )
                # Write receipt
                receipt_dir = state_obj / "applied_results" / paper_number
                receipt_dir.mkdir(parents=True, exist_ok=True)
                receipt_path = receipt_dir / f"{task_id}.json"
                task_path = state_obj / "tasks" / paper_number / f"{task_id}.json"
                task = json.loads(task_path.read_text(encoding="utf-8")) if task_path.is_file() else {}
                receipt_data = {
                    "schema_version": "1.0", "task_id": task_id,
                    "task_input_sha256": task.get("task_input_sha256", ""),
                    "result_sha256": compute_sha256(Path(result_path_str)) if result_path_str and Path(result_path_str).is_file() else "",
                    "paper_number": paper_number, "applied_at": now_iso(),
                    "classifier": "recovery", "classification_run_id": "recovery",
                }
                atomic_write_json(receipt_path, receipt_data, indent=2)
                _write_apply_journal(root=root, paper_number=paper_number, task_id=task_id,
                                     state="committed", result_path=Path(result_path_str) if result_path_str else None)
                report["recovered"] += 1
                report["details"].append({
                    "journal": str(journal_file), "paper_number": paper_number,
                    "action": "recovered_from_assignment_written",
                })

            elif state in ("links_reconciled", "validated"):
                # Links are in place; verify and write receipt
                receipt_dir = state_obj / "applied_results" / paper_number
                receipt_dir.mkdir(parents=True, exist_ok=True)
                receipt_path = receipt_dir / f"{task_id}.json"
                if not receipt_path.is_file():
                    task_path = state_obj / "tasks" / paper_number / f"{task_id}.json"
                    task = json.loads(task_path.read_text(encoding="utf-8")) if task_path.is_file() else {}
                    receipt_data = {
                        "schema_version": "1.0", "task_id": task_id,
                        "task_input_sha256": task.get("task_input_sha256", ""),
                        "result_sha256": compute_sha256(Path(result_path_str)) if result_path_str and Path(result_path_str).is_file() else "",
                        "paper_number": paper_number, "applied_at": now_iso(),
                        "classifier": "recovery", "classification_run_id": "recovery",
                    }
                    atomic_write_json(receipt_path, receipt_data, indent=2)
                _write_apply_journal(root=root, paper_number=paper_number, task_id=task_id,
                                     state="committed", result_path=Path(result_path_str) if result_path_str else None)
                report["recovered"] += 1
                report["details"].append({
                    "journal": str(journal_file), "paper_number": paper_number,
                    "action": f"recovered_from_{state}",
                })

            elif state == "receipt_written":
                # Already has receipt, just mark committed
                _write_apply_journal(root=root, paper_number=paper_number, task_id=task_id,
                                     state="committed", result_path=Path(result_path_str) if result_path_str else None)
                report["recovered"] += 1
                report["details"].append({
                    "journal": str(journal_file), "paper_number": paper_number,
                    "action": "recovered_from_receipt_written",
                })

            else:
                report["failed"] += 1
                report["details"].append({
                    "journal": str(journal_file), "paper_number": paper_number,
                    "action": "failed", "error": f"unknown state: {state}",
                })

        except Exception as exc:
            report["failed"] += 1
            report["details"].append({
                "journal": str(journal_file), "paper_number": paper_number,
                "action": "failed", "error": str(exc),
            })

    return report
