"""Prepare an ignored write/jobs/<job_id> article workspace from all.catalog.

Global all.catalog is a content-only screening index.  The generated
selected_catalog.json is a per-job content-only working snapshot; bibliographic
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

from config.settings import ALL_CATALOG_PATH, PAPERS_DIR, PROJECT_ROOT
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


def _entry_catalog(entry: dict) -> dict:
    """all.catalog v3.1 entries are flat content entries."""
    return entry


def _matches_filter(entry: dict, args: argparse.Namespace) -> bool:
    catalog = _entry_catalog(entry)
    classification = catalog.get("classification") or {}
    screening = catalog.get("screening") or {}

    if args.primary_domain:
        primary = str(classification.get("primary_domain") or "").lower()
        domains = [str(x).lower() for x in (
            classification.get("secondary_domains") or classification.get("domains") or []
        )]
        wanted = args.primary_domain.lower()
        if wanted != primary and wanted not in domains:
            return False

    if args.topic:
        haystack = " ".join(
            str(x)
            for x in (
                (classification.get("topic_tags") or classification.get("topics") or [])
                + (classification.get("methods_tags") or [])
                + (classification.get("phenomena_tags") or [])
                + (classification.get("material_tags") or [])
                + (classification.get("model_tags") or [])
                + (classification.get("keywords_en") or [])
                + (classification.get("keywords_zh") or [])
            )
        ).lower()
        if args.topic.lower() not in haystack:
            return False

    if args.read_decision:
        if str(screening.get("read_decision") or "") != args.read_decision:
            return False

    if args.min_relevance_score is not None:
        try:
            score = float(screening.get("relevance_score"))
        except (TypeError, ValueError):
            return False
        if score < float(args.min_relevance_score):
            return False

    return True


def _is_forbidden_source(path: Path) -> bool:
    """显式安全防护：article 来源必须是正式 papers 目录，禁止 raw/paper_raw/llm_work。

    token 直接以字面量出现以便防回流扫描识别（不是旧 workflow 入口）。
    """
    rel = path.resolve().as_posix().lower()
    forbidden = ("/data/raw", "/data/paper_raw", "/data/llm_work")
    return any(rel.endswith(item) or f"{item}/" in rel for item in forbidden)


def _source_dir_for_entry(entry: dict, papers_dir: Path) -> Path:
    paper_id = str(entry.get("paper_id") or "").strip()
    if not paper_id:
        raise ValueError(f"{entry.get('paper_number')} missing paper_id")
    source = safe_child(papers_dir, paper_id)
    if _is_forbidden_source(source):
        raise ValueError(f"write article source must be formal papers dir, got: {source}")
    if source.exists():
        return source.resolve()
    raise FileNotFoundError(f"formal paper folder not found for {paper_id or entry.get('paper_number')}")


def _compact_selected_entry(entry: dict, source: Path) -> dict:
    """Build one per-job selected_catalog entry (strictly content-only).

    NOTE: this is a WRITE-JOB SNAPSHOT, not the global all.catalog. It keeps
    content fields flat and carries **no** bibliographic metadata and **no**
    path fields — metadata lives in the copied
    ``article/<paper_number>/*.metadata.json`` and path tracking lives in
    ``reports/prepare_article_report.json``. Reads catalog.json (content) from
    the formal paper folder.
    """
    paper_id = str(entry.get("paper_id") or source.name)
    catalog_path = source / f"{paper_id}.catalog.json"
    catalog = {}
    if catalog_path.exists():
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    return {
        "paper_number": str(entry.get("paper_number") or ""),
        "paper_id": paper_id,
        # content (from catalog.json)
        "content_identity": catalog.get("content_identity") or {},
        "classification": catalog.get("classification") or {},
        "screening": catalog.get("screening") or {},
        "research_card": catalog.get("research_card") or {},
        "writing_value": catalog.get("writing_value") or {},
        "evidence_profile": catalog.get("evidence_profile") or {},
        "figure_inventory": catalog.get("figure_inventory") or {},
        "quality_control": catalog.get("quality_control") or {},
    }


def _check_formal_folder(source: Path, paper_id: str) -> None:
    required = [
        source / f"{paper_id}.metadata.json",
        source / f"{paper_id}.catalog.json",
        source / f"{paper_id}.md",
        source / f"{paper_id}.pdf",
        source / "images",
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise FileNotFoundError(f"formal paper folder incomplete for {paper_id}: {missing}")


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
        selected = [p for p in papers if _matches_filter(p, args)]
        selected.sort(
            key=lambda p: (
                float(((_entry_catalog(p).get("screening") or {}).get("relevance_score")) or 0),
                str(p.get("paper_number") or ""),
            ),
            reverse=True,
        )
    if args.limit:
        selected = selected[: args.limit]
    return selected


def prepare_workdir(args: argparse.Namespace) -> dict:
    job_id = validate_job_id(args.job_id or f"article_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    all_catalog_path = Path(args.all_catalog)
    papers_dir = Path(args.papers_dir)
    write_dir = Path(args.write_dir)
    job_dir = safe_child(write_dir, job_id)
    article_dir = safe_child(job_dir, "article")
    reports_dir = safe_child(job_dir, "reports")

    catalog_data = _read_json(all_catalog_path)
    selected = _select_entries(catalog_data, args)
    if not selected:
        raise ValueError("no papers selected")
    if job_dir.exists() and args.apply and not args.overwrite:
        raise FileExistsError(f"write job already exists: {job_dir}")

    planned: list[dict] = []
    for entry in selected:
        paper_number = _validate_paper_number(str(entry.get("paper_number") or ""))
        paper_id = str(entry.get("paper_id") or "").strip()
        if not paper_id:
            raise ValueError(f"{paper_number} missing paper_id")
        source = _source_dir_for_entry(entry, papers_dir)
        _check_formal_folder(source, paper_id)
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
        "source_catalog": normalize_repo_path(all_catalog_path),
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
    parser = argparse.ArgumentParser(description="Prepare write/jobs/<job_id>/article from all.catalog.")
    parser.add_argument("--job-id", default=None)
    parser.add_argument("--paper-numbers", nargs="+", default=None)
    parser.add_argument("--primary-domain", default=None)
    parser.add_argument("--topic", default=None)
    parser.add_argument("--read-decision", default=None)
    parser.add_argument("--min-relevance-score", type=float, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--all-catalog", type=Path, default=Path(ALL_CATALOG_PATH))
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
