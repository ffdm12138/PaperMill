"""Prepare an ignored write job from Catalog-folder-selected formal papers.

The generated selected_catalog.json is a per-job content-only working snapshot; bibliographic
metadata is **not** cached there — citation truth comes from the copied
``article/<paper_number>/*.metadata.json``.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from config.settings import CATALOG_FOLDER_ROOT, PAPERS_DIR, PROJECT_ROOT
from src.catalog_folders.reader import CatalogFolderReader
from src.naming import safe_child, validate_job_id
from src.path_utils import normalize_repo_path
from src.utils.atomic_io import atomic_write_json


WRITE_DIR = PROJECT_ROOT / "write" / "jobs"
_PAPER_NUMBER_RE = re.compile(r"^\d{16}$")


def _read_json(path: Path, default: Any | None = None) -> Any:
    if not path.exists():
        if default is not None:
            return default
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_paper_number(paper_number: str) -> str:
    if not _PAPER_NUMBER_RE.match(str(paper_number or "")):
        raise ValueError(f"invalid paper_number: {paper_number!r}")
    return str(paper_number)


def _selection_sort_key(paper: dict) -> tuple[int, str]:
    decision_rank = {"priority": 0, "read": 1, "pending": 2, "skip": 3}
    return (
        decision_rank.get(str(paper.get("read_decision") or ""), 4),
        str(paper.get("paper_number") or ""),
    )


def _entry_catalog(entry: dict) -> dict:
    """Return one independent Catalog entry."""
    return entry


def _is_forbidden_source(path: Path) -> bool:
    """显式安全防护：article 来源必须是正式 papers 目录，禁止 raw/paper_raw/llm_work。

    token 直接以字面量出现以便防回流扫描识别（不是旧 workflow 入口）。
    """
    rel = path.resolve().as_posix().lower()
    forbidden = ("/data/raw", "/data/paper_raw", "/data/llm_work")
    return any(rel.endswith(item) or f"{item}/" in rel for item in forbidden)


def _source_dir_for_entry(entry: dict) -> Path:
    paper_name = str(entry.get("paper_name") or "").strip()
    if not paper_name:
        raise ValueError(f"{entry.get('paper_number')} missing paper_name")
    source=Path(str(entry.get("formal_directory") or ""))
    if _is_forbidden_source(source):
        raise ValueError(f"write article source must be formal papers dir, got: {source}")
    if source.exists():
        return source.resolve()
    raise FileNotFoundError(f"formal paper folder not found for {paper_name or entry.get('paper_number')}")


def _compact_selected_entry(entry: dict, source: Path) -> dict:
    """Build one per-job selected_catalog entry (strictly content-only).

    This write-job snapshot keeps
    content fields flat and carries **no** bibliographic metadata and **no**
    path fields — metadata lives in the copied
    ``article/<paper_number>/*.metadata.json`` and path tracking lives in
    ``reports/prepare_article_report.json``. Reads catalog.json (content) from
    the formal paper folder.
    """
    paper_name = str(entry.get("paper_name") or source.name)
    catalog_path = source / f"{paper_name}.catalog.json"
    catalog = {}
    if catalog_path.exists():
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    return {
        "paper_number": str(entry.get("paper_number") or ""),
        "paper_name": paper_name,
        # content (from catalog.json)
        "content_identity": catalog.get("content_identity") or {},
        "abstract": catalog.get("abstract") or {},
        "research_context": catalog.get("research_context") or {},
        "methods": catalog.get("methods") or {},
        "data_and_study_design": catalog.get("data_and_study_design") or {},
        "key_findings": catalog.get("key_findings") or [],
        "mechanisms": catalog.get("mechanisms") or [],
        "limitations": catalog.get("limitations") or [],
        "terminology": catalog.get("terminology") or {},
        "figures_and_tables": catalog.get("figures_and_tables") or [],
        "screening": catalog.get("screening") or {},
        "writing_value": catalog.get("writing_value") or {},
    }


def _check_formal_folder(source: Path, paper_name: str) -> None:
    required = [
        source / f"{paper_name}.metadata.json",
        source / f"{paper_name}.catalog.json",
        source / f"{paper_name}.md",
        source / f"{paper_name}.pdf",
        source / "images",
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise FileNotFoundError(f"formal paper folder incomplete for {paper_name}: {missing}")


def _select_entries(catalog_data: dict, args: argparse.Namespace) -> list[dict]:
    papers = list(catalog_data.get("papers") or [])
    if args.paper_numbers:
        wanted = [_validate_paper_number(n) for n in args.paper_numbers]
        by_number = {str(p.get("paper_number")): p for p in papers}
        missing = [n for n in wanted if n not in by_number]
        if missing:
            raise KeyError(f"paper_number not found: {', '.join(missing)}")
        selected = [by_number[n] for n in wanted]
    else:
        selected = papers
        selected.sort(key=_selection_sort_key)
    if args.limit:
        selected = selected[: args.limit]
    return selected


def prepare_workdir(args: argparse.Namespace) -> dict:
    job_id = validate_job_id(args.job_id or f"article_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    catalog_root = Path(args.catalog_root)
    papers_dir = Path(args.papers_dir)
    write_dir = Path(args.write_dir)
    job_dir = safe_child(write_dir, job_id)
    article_dir = safe_child(job_dir, "article")
    reports_dir = safe_child(job_dir, "reports")

    # NOTE: uses custom catalog_root / papers_dir from CLI args; cannot use
    # create_safe_catalog_reader() which hard-codes the global defaults.
    catalog_data={"papers":CatalogFolderReader(root=catalog_root,papers_dir=papers_dir).list_papers(args.categories,mode=args.category_mode)}
    selected = _select_entries(catalog_data, args)
    if not selected:
        raise ValueError("no papers selected")
    if job_dir.exists() and args.apply and not args.overwrite:
        raise FileExistsError(f"write job already exists: {job_dir}")

    planned: list[dict] = []
    for entry in selected:
        paper_number = _validate_paper_number(str(entry.get("paper_number") or ""))
        paper_name = str(entry.get("paper_name") or "").strip()
        if not paper_name:
            raise ValueError(f"{paper_number} missing paper_name")
        source = _source_dir_for_entry(entry)
        _check_formal_folder(source, paper_name)
        target = article_dir / paper_number
        item = _compact_selected_entry(entry, source)
        # Path tracking is kept private here and exposed only in the report
        # (not in selected_catalog, which is strictly content-only).
        item.update({"_source_abs": str(source), "_target_abs": str(target),
                     "status": "planned"})
        planned.append(item)

    def _content_only(item: dict) -> dict:
        return {k: v for k, v in item.items() if not k.startswith("_")}

    def _report_item(item: dict) -> dict:
        out = _content_only(item)
        out["formal_paper_dir"] = normalize_repo_path(Path(item["_source_abs"]))
        out["article_dir"] = normalize_repo_path(Path(item["_target_abs"]))
        return out

    report = {
        "job_id": job_id,
        "write_dir": normalize_repo_path(job_dir),
        "dry_run": not args.apply,
        "selected_count": len(planned),
        "papers": [_report_item(item) for item in planned],
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }

    if not args.apply:
        return report

    if job_dir.exists() and args.overwrite:
        shutil.rmtree(job_dir)
    article_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    for item in planned:
        source = Path(item["_source_abs"])
        target = safe_child(article_dir, item["paper_number"])
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        item["status"] = "copied"

    report["papers"] = [_report_item(item) for item in planned]
    selected_catalog = {
        "schema_version": "1.0",
        "job_id": job_id,
        "source_categories": args.categories or ["all"],
        "papers": [_content_only(item) for item in planned],
    }
    job_json = {
        "schema_version": "1.0",
        "job_id": job_id,
        "workflow": "catalog_tex_article",
        "article_dir": "article",
        "tex_dir": "tex",
        "reports_dir": "reports",
        "created_at": report["created_at"],
        "selected_count": len(planned),
    }
    atomic_write_json(job_dir / "selected_catalog.json", selected_catalog)
    atomic_write_json(job_dir / "job.json", job_json)
    atomic_write_json(reports_dir / "prepare_article_report.json", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare write/jobs/<job_id>/article from Catalog folders.")
    parser.add_argument("--job-id", default=None)
    parser.add_argument("--paper-numbers", nargs="+", default=None)
    parser.add_argument("--categories", nargs="+", default=None)
    parser.add_argument("--category-mode", choices=["union", "intersection"], default="union")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--catalog-root", type=Path, default=Path(CATALOG_FOLDER_ROOT))
    parser.add_argument("--papers-dir", type=Path, default=Path(PAPERS_DIR))
    parser.add_argument("--write-dir", type=Path, default=Path(WRITE_DIR))
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    if args.dry_run:
        args.apply = False
    report = prepare_workdir(args)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
