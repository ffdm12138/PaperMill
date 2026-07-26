"""Real-process smoke tests for discovery operator CLIs."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.test_runtime_workspace import TestRuntimeWorkspace


pytestmark = [pytest.mark.integration, pytest.mark.process]

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = (
    "audit_discovery_workspace_registry.py",
    "repair_discovery_workspaces.py",
    "benchmark_discovery_pipeline.py",
)


@pytest.mark.parametrize("script_name", SCRIPTS)
def test_discovery_operator_cli_help_bootstraps_without_pythonpath(script_name: str):
    with TestRuntimeWorkspace(
        group=f"discovery_cli_{Path(script_name).stem}", repo_root=ROOT,
    ) as workspace:
        env = workspace.child_env()
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script_name), "--help"],
            cwd=workspace.temp_dir,
            env=env,
            capture_output=True,
            text=True,
            shell=False,
            timeout=30,
        )
    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert "usage:" in result.stdout.lower()
