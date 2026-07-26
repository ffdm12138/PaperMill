"""Create a low-friction catalog-first write job workspace."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from config.settings import CATALOG_FOLDER_ROOT, PAPERS_DIR, PROJECT_ROOT
from scripts.prepare_write_article_workdir import prepare_workdir
from src.utils.atomic_io import atomic_write_json, atomic_write_text
from src.utils.naming import safe_child, validate_job_id
from src.utils.path_utils import normalize_repo_path
from src.utils.timestamps import now_iso


WRITE_DIR = PROJECT_ROOT / "write" / "jobs"

# --workflow choice → persisted job.json workflow value.
WORKFLOW_VALUES = {
    "article": "catalog_tex_article",
    "review": "catalog_review",
    "proposal": "catalog_research_proposal",
}

RESEARCH_INPUT_TEMPLATE = """# 研究项目描述

请填写以下各节后再运行 catalog_research_proposal_writer skill；
仍含「（待填）」占位符时 skill 与 check_write_planning_docs.py 会 fail-closed。

## 研究问题

（待填）

## 研究对象与数据

（待填）

## 已有条件与约束

（待填）

## 预期产出

（待填）
"""


def _catalog_value(item: dict[str, Any], *path: str) -> Any:
    cur: Any = item
    for key in path:
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(key)
    return cur


def _paper_title(item: dict[str, Any]) -> str:
    return str(
        _catalog_value(item, "content_identity", "content_title_zh")
        or _catalog_value(item, "metadata", "title", "original")
        or item.get("paper_name")
        or ""
    )


def _selected_summary(report: dict[str, Any]) -> str:
    lines = [
        "# Selected Papers",
        "",
        "| paper_number | paper_name | title | read_decision |",
        "| --- | --- | --- | --- |",
    ]
    for item in report.get("papers") or []:
        lines.append(
            "| {paper_number} | {paper_name} | {title} | {decision} |".format(
                paper_number=item.get("paper_number", ""),
                paper_name=item.get("paper_name", ""),
                title=_paper_title(item).replace("|", "\\|"),
                decision=(item.get("screening") or {}).get("read_decision", ""),
            )
        )
    lines.append("")
    return "\n".join(lines)


def _workflow_next_commands(job_id: str, workflow: str) -> str:
    if workflow == "review":
        return f"""```bash
conda run -n mineru python scripts/export_write_job_bib.py --job-id {job_id}
# invoke the catalog_review_writer skill (matrix -> planning md -> review_plan.json -> tex)
conda run -n mineru python scripts/check_write_planning_docs.py --job-id {job_id}
conda run -n mineru python scripts/check_write_tex_project.py --job-id {job_id} --compile
conda run -n mineru python scripts/check_write_quality_text.py --job-id {job_id}
```"""
    if workflow == "proposal":
        return f"""1. Fill in `input/research_input.md` (all「（待填）」sections).
2. Then:

```bash
conda run -n mineru python scripts/export_write_job_bib.py --job-id {job_id}
# invoke the catalog_research_proposal_writer skill (matrix -> planning md -> proposal_plan.json -> tex)
conda run -n mineru python scripts/check_write_planning_docs.py --job-id {job_id}
conda run -n mineru python scripts/check_write_tex_project.py --job-id {job_id} --compile
conda run -n mineru python scripts/check_write_quality_text.py --job-id {job_id}
```"""
    return f"""```bash
conda run -n mineru python scripts/write_catalog_tex_article.py --job-id {job_id} --title "Mini Review" --language zh --apply
conda run -n mineru python scripts/check_write_tex_project.py --job-id {job_id} --compile
conda run -n mineru python scripts/check_write_quality_text.py --job-id {job_id}
```"""


def _job_readme(job_id: str, selected_count: int, workflow: str = "article") -> str:
    return f"""# Write Job {job_id}

This job was created by `scripts/create_write_job.py`.

- selected papers: {selected_count}
- workflow: {WORKFLOW_VALUES[workflow]}
- article workspace: `article/<paper_number>/`
- status: prepared
- quality accepted: no

Next commands:

{_workflow_next_commands(job_id, workflow)}

Rules:

- Write only from the copied `article/` workspace.
- Do not read `data/papers` directly while writing TeX.
- Do not commit this `write/jobs/{job_id}/` runtime directory.
- Passing scaffold generation does not mean quality acceptance.
"""


def create_write_job(args: argparse.Namespace) -> dict[str, Any]:
    if args.job_id:
        validate_job_id(args.job_id)
    prepare_args = argparse.Namespace(
        job_id=args.job_id,
        paper_numbers=args.paper_numbers,
        categories=args.categories,
        category_mode=args.category_mode,
        limit=args.limit,
        apply=True,
        dry_run=False,
        overwrite=args.overwrite,
        catalog_root=Path(args.catalog_root),
        papers_dir=Path(args.papers_dir),
        write_dir=Path(args.write_dir),
    )
    report = prepare_workdir(prepare_args)
    job_id = str(report["job_id"])
    job_dir = safe_child(Path(args.write_dir), job_id)
    reports_dir = safe_child(job_dir, "reports")
    reports_dir.mkdir(parents=True, exist_ok=True)

    # job.json carries the workflow switch every downstream gate reads, so it is
    # written before the descriptive files: an interrupted run then still shows
    # the requested workflow rather than the default one.
    workflow_value = WORKFLOW_VALUES[args.workflow]
    if args.workflow != "article":
        job_json_path = job_dir / "job.json"
        job_meta = json.loads(job_json_path.read_text(encoding="utf-8"))
        job_meta["workflow"] = workflow_value
        atomic_write_json(job_json_path, job_meta, indent=2)
    if args.workflow == "proposal":
        input_dir = safe_child(job_dir, "input")
        input_dir.mkdir(parents=True, exist_ok=True)
        research_input = input_dir / "research_input.md"
        if not research_input.exists():
            atomic_write_text(research_input, RESEARCH_INPUT_TEMPLATE)

    readme_path = job_dir / "README.md"
    summary_path = reports_dir / "selected_papers.md"
    atomic_write_text(
        readme_path,
        _job_readme(job_id, int(report.get("selected_count") or 0), args.workflow),
    )
    atomic_write_text(summary_path, _selected_summary(report))

    result = {
        "job_id": job_id,
        "status": "prepared",
        "quality_status": "not_accepted",
        "workflow": workflow_value,
        "selected_count": report.get("selected_count", 0),
        "job_dir": normalize_repo_path(job_dir),
        "readme": normalize_repo_path(readme_path),
        "selected_summary": normalize_repo_path(summary_path),
        "created_at": now_iso(),
    }
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a catalog-first write job.")
    parser.add_argument("--job-id", default=None)
    parser.add_argument("--workflow", choices=sorted(WORKFLOW_VALUES), default="article")
    parser.add_argument("--paper-numbers", nargs="+", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--categories", nargs="+", default=None)
    parser.add_argument("--category-mode", choices=["union", "intersection"], default="union")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--catalog-root", type=Path, default=Path(CATALOG_FOLDER_ROOT))
    parser.add_argument("--papers-dir", type=Path, default=Path(PAPERS_DIR))
    parser.add_argument("--write-dir", type=Path, default=Path(WRITE_DIR))
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
    result = create_write_job(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
