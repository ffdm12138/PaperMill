"""Contract: --stage-to-paper-raw requires --apply (or --dry-run)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_stage_to_paper_raw_without_apply_rejected():
    """--stage-to-paper-raw alone must fail at argument parsing."""
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(repo / "scripts" / "discover_papers.py"),
         "--stage-to-paper-raw", "--keyword-zh", "test"],
        capture_output=True, text=True,
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode != 0
    assert "stage-to-paper-raw requires --apply" in result.stderr


def test_stage_to_paper_raw_with_apply_accepted_argparse():
    """--stage-to-paper-raw --apply must pass argument validation."""
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(repo / "scripts" / "discover_papers.py"),
         "--stage-to-paper-raw", "--apply", "--keyword-zh", "test"],
        capture_output=True, text=True,
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )
    # Should fail later (no real network), but NOT at argument parsing.
    assert "stage-to-paper-raw requires --apply" not in result.stderr
    # returncode may be non-zero due to missing config, but not from argparse


def test_stage_to_paper_raw_with_dry_run_accepted_argparse():
    """--stage-to-paper-raw --dry-run must pass argument validation."""
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(repo / "scripts" / "discover_papers.py"),
         "--stage-to-paper-raw", "--dry-run", "--keyword-zh", "test"],
        capture_output=True, text=True,
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert "stage-to-paper-raw requires --apply" not in result.stderr
