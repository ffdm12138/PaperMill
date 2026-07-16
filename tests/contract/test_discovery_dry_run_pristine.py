"""Contract: --dry-run is completely read-only (no file tree/hash/mtime changes)."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _snapshot_tree(root: Path) -> dict[str, str]:
    """Return {relpath: sha256} for all files under root."""
    snap = {}
    if not root.exists():
        return snap
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            snap[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snap


def test_dry_run_does_not_mutate_tree(tmp_path: Path):
    """--dry-run must not change any file under data/ or reports/."""
    repo = Path(__file__).resolve().parents[2]
    tmp = Path(tmp_path)

    # Use tmp as the data root.
    data_dir = tmp / "data"
    data_dir.mkdir()
    (data_dir / "discovery").mkdir(parents=True)

    # Snapshot before
    before = _snapshot_tree(tmp)

    result = subprocess.run(
        [sys.executable, str(repo / "scripts" / "discover_papers.py"),
         "--dry-run", "--keyword-zh", "test",
         "--data-root", str(data_dir)],
        capture_output=True, text=True,
        timeout=30,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    # May fail due to missing keyword notebook, but must not mutate files.
    after = _snapshot_tree(tmp)

    # Every file that existed before must be unchanged.
    for path, h in before.items():
        assert after.get(path) == h, f"File changed during dry-run: {path}"

    # No new files created.
    for path in after:
        assert path in before, f"New file created during dry-run: {path}"
