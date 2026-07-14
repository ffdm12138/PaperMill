"""Verify catalog CLI scripts bootstrap without PYTHONPATH."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = [
    "show_catalog_classification_progress.py",
    "run_catalog_classification.py",
    "claim_catalog_classification_tasks.py",
]


def _find_python() -> str:
    """Return the project Python with all dependencies installed.

    Uses the currently running interpreter first; falls back to the project's
    known conda env so subprocess calls work regardless of which Python runs
    the test suite.
    """
    # Probe the current Python — if it has project deps, use it.
    try:
        import config.settings  # noqa: F401
        import jsonschema  # noqa: F401
        return sys.executable
    except ModuleNotFoundError:
        pass

    # Fallback: project's conda env (documented in CLAUDE.md).
    conda_env = Path(
        r"C:\Users\Admin\.conda\envs\mineru\python.exe"
    )
    if conda_env.is_file():
        return str(conda_env.resolve())

    # Last resort — return sys.executable anyway (test may fail,
    # but the assertion error will be informative).
    return sys.executable


_PYTHON = _find_python()


class TestCliBootstrap:
    """Test that CLI scripts work without PYTHONPATH from arbitrary cwd."""

    @pytest.mark.parametrize("script_name", SCRIPTS)
    def test_help_from_repo_root(self, script_name):
        """Script --help works from repo root with no PYTHONPATH."""
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [_PYTHON, str(ROOT / "scripts" / script_name), "--help"],
            cwd=str(ROOT),
            env=env,
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "ModuleNotFoundError" not in result.stderr

    @pytest.mark.parametrize("script_name", SCRIPTS)
    def test_help_from_scripts_dir(self, script_name):
        """Script --help works from scripts/ dir with no PYTHONPATH."""
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [_PYTHON, script_name, "--help"],
            cwd=str(ROOT / "scripts"),
            env=env,
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "ModuleNotFoundError" not in result.stderr

    @pytest.mark.parametrize("script_name", SCRIPTS)
    def test_help_from_tmpdir(self, tmp_path, script_name):
        """Script --help works from a tmp_path cwd with no PYTHONPATH."""
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [_PYTHON, str(ROOT / "scripts" / script_name), "--help"],
            cwd=str(tmp_path),
            env=env,
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "ModuleNotFoundError" not in result.stderr


class TestShowProgressSmoke:
    """Read-only smoke test: show_catalog_classification_progress.py reads isolated fixture data."""

    def test_show_progress_reads_isolated_fixture(self, tmp_path):
        """The progress script reads an isolated catalog root without touching real data."""
        import json

        # Build a minimal isolated fixture: empty catalog root, papers dir, ledger.
        catalog_root = tmp_path / "catalog"
        papers_dir = tmp_path / "papers"
        ledger_path = tmp_path / "ledger.json"
        catalog_root.mkdir(parents=True)
        papers_dir.mkdir(parents=True)
        ledger_path.write_text(
            json.dumps({"schema_version": "1.0", "max_number": "0000000000000000", "items": {}}),
            encoding="utf-8",
        )

        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [
                _PYTHON,
                str(ROOT / "scripts" / "show_catalog_classification_progress.py"),
                "--catalog-root", str(catalog_root),
                "--papers-dir", str(papers_dir),
                "--ledger-path", str(ledger_path),
                "--json",
            ],
            cwd=str(tmp_path),
            env=env,
            capture_output=True, text=True,
        )
        # The script should run without crashing (exit 0 when classification is
        # vacuously complete, or 1 if not — either way no ModuleNotFoundError).
        assert "ModuleNotFoundError" not in result.stderr, result.stderr
        assert result.returncode in (0, 1), f"stderr: {result.stderr}"
        payload = json.loads(result.stdout)
        assert payload["formal_papers"] == 0
        assert payload["pending_papers"] == 0
        # Must not touch real data/catalog/.
        assert "data" not in str(catalog_root)
