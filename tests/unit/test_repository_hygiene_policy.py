from pathlib import Path

import pytest

from src.services.repository_hygiene import (
    is_allowed_runtime_placeholder,
    is_forbidden_git_member,
    is_forbidden_snapshot_member,
    normalize_repo_rel_path,
)


@pytest.mark.parametrize("value,expected", [
    (r".workbuddy\\memory\x.md", ".workbuddy/memory/x.md"),
    ("./data//PAPERS//x.md", "data/papers/x.md"),
])
def test_normalize_repo_rel_path(value, expected):
    assert normalize_repo_rel_path(value) == expected


@pytest.mark.parametrize("path", [
    ".workbuddy/memory/x.md", ".reasonix/x.md", "data/cleanup_report.json",
    "data/paper_raw/0000000000000001/paper.pdf", "data/papers/example/paper.md",
    "data/staging/x", "data/transactions/commit/x.json",
])
def test_runtime_paths_are_forbidden_for_git_and_snapshot(path):
    assert is_forbidden_git_member(path)
    assert is_forbidden_snapshot_member(path)


@pytest.mark.parametrize("path", [
    "data/paper_raw/.gitkeep", "data/papers/.gitkeep", "data/transactions/.gitkeep",
])
def test_explicit_runtime_placeholders_are_allowed(path):
    assert is_allowed_runtime_placeholder(path)
    assert not is_forbidden_git_member(path)
    assert not is_forbidden_snapshot_member(path)
