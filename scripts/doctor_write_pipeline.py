"""Diagnose the catalog-first writer environment and optional write job."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from config.settings import CATALOG_FOLDER_ROOT, PROJECT_ROOT
from scripts.check_write_quality_text import check_write_quality_text
from scripts.check_write_tex_project import check_tex_project
from src.utils.naming import safe_child, validate_job_id
from src.utils.timestamps import now_iso
from src.utils.path_utils import normalize_repo_path


WRITE_DIR = PROJECT_ROOT / "write" / "jobs"
KEY_SCRIPTS = (
    "scripts/prepare_write_article_workdir.py",
    "scripts/write_catalog_tex_article.py",
    "scripts/check_write_tex_project.py",
    "scripts/check_write_quality_text.py",
)


def _git_ls_files(repo_root: Path, path: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", path],
        cwd=repo_root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()]


def _check_write_jobs_tracking(repo_root: Path) -> dict[str, Any]:
    tracked = _git_ls_files(repo_root, "write/jobs")
    allowed = {"write/jobs/.gitkeep"}
    unexpected = sorted(path for path in tracked if path not in allowed)
    return {
        "ok": not unexpected,
        "tracked": tracked,
        "unexpected": unexpected,
    }


def _check_key_scripts(repo_root: Path) -> list[dict[str, Any]]:
    return [
        {"path": path, "exists": (repo_root / path).exists()}
        for path in KEY_SCRIPTS
    ]


def _check_tex_compiler() -> dict[str, Any]:
    latexmk = shutil.which("latexmk")
    xelatex = shutil.which("xelatex")
    pdflatex = shutil.which("pdflatex")
    return {
        "available": bool(latexmk or xelatex),
        "latexmk": latexmk or "",
        "xelatex": xelatex or "",
        "pdflatex": pdflatex or "",
    }


def _existing_reports(reports_dir: Path) -> dict[str, bool]:
    names = [
        "prepare_article_report.json",
        "write_article_report.json",
        "format_check.json",
        "write_quality_check_report.json",
    ]
    return {name: (reports_dir / name).exists() for name in names}


def _job_status(job_dir: Path, tex_report: dict | None, quality_report: dict | None) -> str:
    if not job_dir.exists():
        return "missing"
    if quality_report and quality_report.get("valid"):
        return "quality_accepted"
    if tex_report and tex_report.get("valid"):
        return "mechanically_valid"
    return "prepared"


def _check_job(job_id: str, write_dir: Path) -> dict[str, Any]:
    job_id = validate_job_id(job_id)
    job_dir = safe_child(write_dir, job_id)
    article_dir = job_dir / "article"
    tex_dir = job_dir / "tex"
    reports_dir = job_dir / "reports"
    tex_report: dict | None = None
    quality_report: dict | None = None
    errors: list[str] = []

    if tex_dir.exists() and any(tex_dir.rglob("*.tex")):
        try:
            tex_report = check_tex_project(
                argparse.Namespace(job_id=job_id, compile=False, write_dir=write_dir)
            )
        except Exception as exc:  # pragma: no cover - defensive report path
            errors.append(f"check_write_tex_project failed: {exc}")

        try:
            quality_report = check_write_quality_text(
                argparse.Namespace(job_id=job_id, write_dir=write_dir)
            )
        except Exception as exc:  # pragma: no cover - defensive report path
            errors.append(f"check_write_quality_text failed: {exc}")

    return {
        "job_id": job_id,
        "status": _job_status(job_dir, tex_report, quality_report),
        "exists": job_dir.exists(),
        "job_dir": normalize_repo_path(job_dir) if job_dir.exists() else str(job_dir),
        "article_exists": article_dir.exists(),
        "tex_exists": tex_dir.exists(),
        "reports_exists": reports_dir.exists(),
        "reports": _existing_reports(reports_dir) if reports_dir.exists() else {},
        "tex_project": tex_report,
        "quality_text": quality_report,
        "errors": errors,
    }


def doctor_write_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root)
    catalog_root = Path(args.catalog_root)
    write_dir = Path(args.write_dir)

    key_scripts = _check_key_scripts(repo_root)
    tracked_jobs = _check_write_jobs_tracking(repo_root)
    tex_compiler = _check_tex_compiler()
    job = _check_job(args.job_id, write_dir) if args.job_id else None

    errors: list[str] = []
    warnings: list[str] = []
    runtime_ready = (catalog_root / "all").exists() and not (catalog_root / ".state" / "DIRTY").exists()
    if not runtime_ready:
        warnings.append(
            f"{catalog_root} is not a ready Catalog-folder view. "
            "Run python scripts/reconcile_catalog_folders.py --apply after papers are present."
        )
    if not tracked_jobs["ok"]:
        errors.append("write/jobs has tracked runtime files")
    missing_scripts = [item["path"] for item in key_scripts if not item["exists"]]
    if missing_scripts:
        errors.append("missing writer scripts: " + ", ".join(missing_scripts))
    if args.job_id and job and job["status"] == "missing":
        errors.append(f"write job not found: {args.job_id}")
    if args.job_id and job and job["errors"]:
        errors.extend(job["errors"])

    source_valid = not errors
    report = {
        "schema_version": "1.0",
        "checked_at": now_iso(),
        "valid": source_valid,
        "source_valid": source_valid,
        "runtime_ready": runtime_ready,
        "errors": errors,
        "warnings": warnings,
        "environment": {
            "catalog_folders": {
                "path": normalize_repo_path(catalog_root) if catalog_root.exists() else str(catalog_root),
                "ready": runtime_ready,
            },
            "write_jobs_tracking": tracked_jobs,
            "key_scripts": key_scripts,
            "tex_compiler": tex_compiler,
        },
        "job": job,
    }
    return report


def _print_human(report: dict[str, Any]) -> None:
    print(
        f"valid={str(report['valid']).lower()} "
        f"source_valid={str(report.get('source_valid', report['valid'])).lower()} "
        f"runtime_ready={str(report.get('runtime_ready', False)).lower()} "
        f"errors={len(report['errors'])} warnings={len(report.get('warnings') or [])}"
    )
    env = report["environment"]
    print(f"catalog_folders_ready={env['catalog_folders']['ready']}")
    print(f"write_jobs_tracking_ok={env['write_jobs_tracking']['ok']}")
    print(f"tex_compiler_available={env['tex_compiler']['available']}")
    if report.get("job"):
        job = report["job"]
        print(f"job_id={job['job_id']} status={job['status']}")
        print(f"article={job['article_exists']} tex={job['tex_exists']} reports={job['reports_exists']}")
    for error in report["errors"]:
        print(f"ERROR: {error}")
    for warning in report.get("warnings") or []:
        print(f"WARNING: {warning}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Diagnose writer pipeline environment and jobs.")
    parser.add_argument("--job-id", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--catalog-root", type=Path, default=Path(CATALOG_FOLDER_ROOT))
    parser.add_argument("--write-dir", type=Path, default=Path(WRITE_DIR))
    parser.add_argument("--repo-root", type=Path, default=Path(PROJECT_ROOT))
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    report = doctor_write_pipeline(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
