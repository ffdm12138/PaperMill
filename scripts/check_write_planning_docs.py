"""Deterministic checker for review/proposal planning intermediates.

Validates the Markdown handoff documents and the machine-checkable plan JSON
produced by the ``catalog_review_writer`` / ``catalog_research_proposal_writer``
skills inside one ``write/jobs/<job_id>/`` workspace:

- required intermediates exist, are non-empty, and carry no placeholder marks;
- the plan JSON validates against the skill-shipped schema;
- the plan's paper pool exactly matches ``selected_catalog.json``;
- ``reports/literature_matrix.md`` has one row per selected paper;
- every referenced ``bib_key`` exists in ``tex/references.bib``;
- every evidence reference points into the paper pool;
- proposal only: ``input/research_input.md`` is filled in (no「（待填）」),
  every ``results_plan`` item stays ``planned``, method references resolve.

Writes ``reports/planning_docs_check_report.json``; exit 1 on any error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

import jsonschema

from config.settings import PROJECT_ROOT
from src.utils.atomic_io import atomic_write_json
from src.utils.naming import safe_child, validate_job_id
from src.utils.timestamps import now_iso
from src.writer.bib import parse_blocks
from src.writer.safe_write import TODO_MARKERS

WRITE_DIR = PROJECT_ROOT / "write" / "jobs"

PROFILES = {
    "catalog_review": {
        "profile": "review",
        "plan_file": "planning/review_plan.json",
        "schema": PROJECT_ROOT / "skills" / "catalog_review_writer" / "review_plan_schema.json",
        "intermediates": [
            "reports/literature_matrix.md",
            "planning/review_outline.md",
            "planning/research_gaps.md",
            "planning/proposed_directions.md",
        ],
    },
    "catalog_research_proposal": {
        "profile": "proposal",
        "plan_file": "planning/proposal_plan.json",
        "schema": PROJECT_ROOT / "skills" / "catalog_research_proposal_writer" / "proposal_plan_schema.json",
        "intermediates": [
            "reports/literature_matrix.md",
            "planning/review_outline.md",
            "planning/research_gaps.md",
            "planning/methods_design.md",
            "planning/results_plan.md",
        ],
    },
}


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _check_intermediate(job_dir: Path, rel: str, errors: list[str]) -> None:
    path = job_dir / rel
    if not path.exists():
        errors.append(f"missing intermediate: {rel}")
        return
    text = path.read_text(encoding="utf-8")
    if len(text.strip()) < 40:
        errors.append(f"intermediate looks empty: {rel}")
        return
    for marker in TODO_MARKERS:
        if marker in text:
            errors.append(f"{rel}: placeholder marker {marker!r} present")
            return


def _plan_evidence_refs(plan: dict) -> list[dict]:
    refs: list[dict] = []
    for theme in plan.get("themes") or []:
        refs.extend(theme.get("evidence") or [])
    for gap in plan.get("research_gaps") or []:
        refs.extend(gap.get("evidence") or [])
    for direction in plan.get("proposed_directions") or []:
        refs.extend(direction.get("builds_on") or [])
    for method in plan.get("methods_design") or []:
        refs.extend(method.get("grounded_in") or [])
    return refs


def check_planning_docs(args: argparse.Namespace) -> dict:
    job_id = validate_job_id(args.job_id)
    job_dir = safe_child(Path(args.write_dir), job_id)
    errors: list[str] = []

    job_meta_path = job_dir / "job.json"
    if not job_meta_path.exists():
        return {"job_id": job_id, "passed": False,
                "errors": [f"job.json not found: {job_meta_path}"]}
    workflow = str(_read_json(job_meta_path).get("workflow") or "")
    if args.profile:
        workflow = {
            "review": "catalog_review",
            "proposal": "catalog_research_proposal",
        }[args.profile]
    spec = PROFILES.get(workflow)
    if spec is None:
        return {"job_id": job_id, "passed": False, "errors": [
            f"job workflow {workflow!r} is not a planning-docs workflow "
            "(expected catalog_review or catalog_research_proposal; "
            "override with --profile)"
        ]}

    for rel in spec["intermediates"]:
        _check_intermediate(job_dir, rel, errors)

    if spec["profile"] == "proposal":
        research_input = job_dir / "input" / "research_input.md"
        if not research_input.exists():
            errors.append("missing input/research_input.md")
        elif "（待填）" in research_input.read_text(encoding="utf-8"):
            errors.append("input/research_input.md still contains「（待填）」placeholders")

    selected_path = job_dir / "selected_catalog.json"
    selected_numbers: set[str] = set()
    if not selected_path.exists():
        errors.append("missing selected_catalog.json")
    else:
        selected_numbers = {
            str(item.get("paper_number") or "")
            for item in _read_json(selected_path).get("papers") or []
        }

    plan_path = job_dir / spec["plan_file"]
    plan: dict = {}
    if not plan_path.exists():
        errors.append(f"missing plan: {spec['plan_file']}")
    else:
        plan = _read_json(plan_path)
        schema = _read_json(spec["schema"])
        try:
            jsonschema.validate(instance=plan, schema=schema)
        except jsonschema.ValidationError as exc:
            errors.append(f"{spec['plan_file']}: schema violation: {exc.message}")

    if plan and selected_numbers:
        pool = {str(p.get("paper_number") or "") for p in plan.get("paper_pool") or []}
        if pool != selected_numbers:
            missing = sorted(selected_numbers - pool)
            extra = sorted(pool - selected_numbers)
            errors.append(
                f"paper_pool mismatch vs selected_catalog.json "
                f"(missing: {missing or 'none'}; extra: {extra or 'none'})"
            )

        matrix_path = job_dir / "reports" / "literature_matrix.md"
        if matrix_path.exists():
            matrix_text = matrix_path.read_text(encoding="utf-8")
            for number in sorted(selected_numbers):
                if number not in matrix_text:
                    errors.append(f"literature_matrix.md: no row for {number}")

        bib_path = job_dir / "tex" / "references.bib"
        if not bib_path.exists():
            errors.append("missing tex/references.bib — run scripts/export_write_job_bib.py first")
        else:
            bib_keys = set(parse_blocks(bib_path.read_text(encoding="utf-8")))
            plan_keys = {str(p.get("bib_key") or "") for p in plan.get("paper_pool") or []}
            plan_keys.update(str(r.get("bib_key") or "") for r in _plan_evidence_refs(plan))
            for key in sorted(plan_keys - bib_keys):
                errors.append(f"plan bib_key not in references.bib: {key}")

        pool_numbers = {str(p.get("paper_number") or "") for p in plan.get("paper_pool") or []}
        for ref in _plan_evidence_refs(plan):
            number = str(ref.get("paper_number") or "")
            if number not in pool_numbers:
                errors.append(f"evidence paper_number outside paper_pool: {number}")

        gap_ids = {str(g.get("gap_id") or "") for g in plan.get("research_gaps") or []}
        for direction in plan.get("proposed_directions") or []:
            for gap_id in direction.get("addresses_gap_ids") or []:
                if gap_id not in gap_ids:
                    errors.append(f"direction references unknown gap: {gap_id}")

        if spec["profile"] == "proposal":
            method_ids = {str(m.get("method_id") or "") for m in plan.get("methods_design") or []}
            for item in plan.get("results_plan") or []:
                if item.get("status") != "planned":
                    errors.append(
                        f"results_plan {item.get('analysis_id')}: status must stay 'planned'"
                    )
                for method_id in item.get("uses_method_ids") or []:
                    if method_id not in method_ids:
                        errors.append(
                            f"results_plan {item.get('analysis_id')}: unknown method {method_id}"
                        )

    result = {
        "schema_version": "1.0",
        "job_id": job_id,
        "workflow": workflow,
        "passed": not errors,
        "errors": errors,
        "checked_at": now_iso(),
    }
    reports_dir = safe_child(job_dir, "reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(reports_dir / "planning_docs_check_report.json", result, indent=2)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate review/proposal planning intermediates for one write job."
    )
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--write-dir", type=Path, default=Path(WRITE_DIR))
    parser.add_argument("--profile", choices=["review", "proposal"], default=None,
                        help="override workflow detection from job.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    result = check_planning_docs(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
