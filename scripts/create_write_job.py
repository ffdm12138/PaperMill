"""Create a low-friction catalog-first write job workspace."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import CATALOG_FOLDER_ROOT, PAPERS_DIR, PROJECT_ROOT
from scripts.prepare_write_article_workdir import prepare_workdir
from src.naming import safe_child, validate_job_id
from src.path_utils import normalize_repo_path


WRITE_DIR = PROJECT_ROOT / "write" / "jobs"


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


def _job_readme(job_id: str, selected_count: int) -> str:
    return f"""# Write Job {job_id}

This job was created by `scripts/create_write_job.py`.

- selected papers: {selected_count}
- article workspace: `article/<paper_number>/`
- status: prepared
- quality accepted: no

Next commands:

```bash
conda run -n mineru python scripts/write_catalog_tex_article.py --job-id {job_id} --title "Mini Review" --language zh --apply
conda run -n mineru python scripts/check_write_tex_project.py --job-id {job_id} --compile
conda run -n mineru python scripts/check_write_quality_text.py --job-id {job_id}
```

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

    readme_path = job_dir / "README.md"
    summary_path = reports_dir / "selected_papers.md"
    readme_path.write_text(_job_readme(job_id, int(report.get("selected_count") or 0)), encoding="utf-8")
    summary_path.write_text(_selected_summary(report), encoding="utf-8")

    result = {
        "job_id": job_id,
        "status": "prepared",
        "quality_status": "not_accepted",
        "selected_count": report.get("selected_count", 0),
        "job_dir": normalize_repo_path(job_dir),
        "readme": normalize_repo_path(readme_path),
        "selected_summary": normalize_repo_path(summary_path),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a catalog-first write job.")
    parser.add_argument("--job-id", default=None)
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
    args = build_parser().parse_args(argv)
    result = create_write_job(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
