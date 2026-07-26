"""Run local ingest-pipeline health checks without importing papers."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from config.settings import PAPER_RAW_DIR, PROJECT_ROOT
from src.utils.atomic_io import atomic_write_json


DEFAULT_REPORT_PATH = PROJECT_ROOT / "reports" / "doctor_ingest_pipeline_report.json"
INGEST_TESTS = [
    "tests/contract/test_import_metadata_gates.py",
    "tests/integration/test_paper_raw_metadata_resolver.py",
    "tests/contract/test_paper_raw_preflight.py",
    "tests/integration/test_v2_library.py",
    "tests/contract/test_catalog_metadata_separation.py",
    "tests/integration/test_stage_raw_pdfs.py",
]


def _has_paper_raw_sources(root: Path) -> bool:
    return root.exists() and any(
        p.is_dir() and p.name.isdigit() and len(p.name) == 16
        for p in root.iterdir()
    )


def _pytest_env() -> dict[str, str]:
    """Isolated environment for the pytest subprocess.

    Disables entry-point plugin auto-loading so third-party plugins installed
    in the environment cannot change warning/asyncio/coverage/tracing behavior
    or cause the pytest process to hang after tests pass. conftest.py files,
    ``-p`` options, and ``PYTEST_PLUGINS`` are unaffected. Mirrors
    ``scripts/agent_acceptance.py``.
    """
    env = os.environ.copy()
    env.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _run_step(
    name: str,
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    run_env = env if env is not None else os.environ.copy()
    run_env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=run_env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "name": name,
            "command": cmd,
            "returncode": 124,
            "blocking": True,
            "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "") if isinstance(exc.stderr, str) else "",
            "error": f"timed out after {timeout}s",
        }
    return {
        "name": name,
        "command": cmd,
        "returncode": proc.returncode,
        "blocking": proc.returncode != 0,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def build_report(*, run_tests: bool, paper_raw_dir: Path, project_root: Path) -> dict[str, Any]:
    py = sys.executable
    steps: list[dict[str, Any]] = []
    base_steps = [
        ("check_directory_hygiene", [py, "scripts/check_directory_hygiene.py"]),
        ("validate_v2_library", [py, "scripts/validate_v2_library.py"]),
        ("audit_metadata_quality", [py, "scripts/audit_metadata_quality.py"]),
        ("audit_ingest_duplicates", [py, "scripts/audit_ingest_duplicates.py", "--strict"]),
    ]
    for name, cmd in base_steps:
        steps.append(_run_step(name, cmd, cwd=project_root))

    if _has_paper_raw_sources(paper_raw_dir):
        steps.append(_run_step(
            "preflight_paper_raw_import",
            [py, "scripts/preflight_paper_raw_import.py", "--all", "--strict"],
            cwd=project_root,
        ))
    else:
        steps.append({
            "name": "preflight_paper_raw_import",
            "command": [py, "scripts/preflight_paper_raw_import.py", "--all", "--strict"],
            "returncode": 0,
            "blocking": False,
            "skipped": True,
            "reason": "no data/paper_raw/<paper_number> sources",
            "stdout": "",
            "stderr": "",
        })

    if run_tests:
        steps.append(_run_step(
            "pytest_ingest_subset",
            [py, "-m", "pytest", "-q", *INGEST_TESTS],
            cwd=project_root,
            env=_pytest_env(),
            timeout=300,
        ))

    blocking = [step for step in steps if step.get("blocking")]
    return {
        "schema_version": "1.0",
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "valid": not blocking,
        "blocking_count": len(blocking),
        "steps": steps,
    }


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Doctor the local v2 ingest pipeline without importing papers.")
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--skip-tests", action="store_true", help="skip the ingest-related pytest subset")
    args = parser.parse_args(argv)

    report = build_report(
        run_tests=not args.skip_tests,
        paper_raw_dir=args.paper_raw_dir,
        project_root=args.project_root,
    )
    atomic_write_json(args.report_path, report, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["blocking_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
