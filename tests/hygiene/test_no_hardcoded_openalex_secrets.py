"""Hygiene: OpenAlex credentials must never be hardcoded in the repository.

Reuses the production secret scanner from ``pack_repo`` rather than
maintaining a separate set of regex rules.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.hygiene

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OPENALEX_ALLOWED_PREFIXES = ("docs/", "tests/")
OPENALEX_ALLOWED_FILES = {
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "scripts/pack_repo.py",
    "src/fetch/openalex_credentials.py",
}
SCAN_SUFFIXES = {".py", ".md", ".txt", ".ps1", ".bat", ".sh", ".yaml", ".yml", ".toml"}

# Import the production scanner from pack_repo
sys.path.insert(0, str(PROJECT_ROOT))
from scripts import pack_repo as pr  # noqa: E402


def _git_tracked_files() -> list[str]:
    """Return all git-tracked files relative to PROJECT_ROOT."""
    result = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "ls-files", "--cached", "-z"],
        capture_output=True, text=True, encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("git ls-files failed")
    return [f for f in result.stdout.split("\0") if f]


def test_no_hardcoded_openalex_credentials_in_tracked_files():
    """Scan all git-tracked files using the production secret scanner.

    Any finding indicates a real credential literal in the repository.
    Test files are excluded because they legitimately contain fake
    credential values for unit-test purposes.
    """
    files = [f for f in _git_tracked_files() if not f.startswith("tests/")]
    findings = pr.scan_files_for_secrets(files)
    assert not findings, (
        f"Found {len(findings)} secret-like literal(s) in tracked files:\n"
        + "\n".join(
            f"  {f.path}: {f.rule} (line {f.line})"
            for f in findings[:20]
        )
        + ("\n  ..." if len(findings) > 20 else "")
    )


def test_openalex_consumers_use_centralized_module():
    """Both OpenAlex consumers must import from openalex_credentials,
    not access os.environ directly.
    """
    consumer_files = [
        PROJECT_ROOT / "src" / "discovery" / "search_openalex.py",
        PROJECT_ROOT / "src" / "fetch" / "fetch_openalex.py",
    ]
    for path in consumer_files:
        text = path.read_text(encoding="utf-8")
        # Must import from the centralized module
        assert "from src.fetch.openalex_credentials import" in text, (
            f"{path.name} does not import from openalex_credentials"
        )
        # Must NOT access os.environ directly for credential vars
        assert "os.environ" not in text, (
            f"{path.name} still uses os.environ directly"
        )


def _iter_repo_text_files() -> list[Path]:
    denied_parts = {".git", "__pycache__", ".pytest_cache", "output", "images", "logs", "cache"}
    files: list[Path] = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(PROJECT_ROOT).parts
        if any(part in denied_parts for part in rel_parts):
            continue
        if path.suffix.lower() in SCAN_SUFFIXES or path.name in OPENALEX_ALLOWED_FILES:
            files.append(path)
    return files


def test_openalex_env_names_only_appear_in_allowed_files():
    """OPENALEX_* names must not reappear as hidden constants/default args."""
    offenders: list[str] = []
    for path in _iter_repo_text_files():
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "OPENALEX_EMAIL" not in text and "OPENALEX_API_KEY" not in text:
            continue
        allowed = rel in OPENALEX_ALLOWED_FILES or rel.startswith(OPENALEX_ALLOWED_PREFIXES)
        if not allowed:
            offenders.append(rel)
    assert not offenders, "OPENALEX credential env names outside allowlist:\n" + "\n".join(offenders)
