from pathlib import Path

from src.path_utils import (
    is_windows_abs_path,
    normalize_record_paths,
    normalize_repo_path,
    resolve_stored_path,
)


def test_is_windows_abs_path_detects_drive_letter():
    assert is_windows_abs_path(r"E:\1\mineru\data\raw\a.pdf")
    assert is_windows_abs_path("e:/1/mineru/data/raw/a.pdf")
    assert not is_windows_abs_path("data/raw/a.pdf")


def test_normalize_repo_path_converts_project_absolute_to_relative(tmp_path):
    root = tmp_path / "repo"
    target = root / "data" / "raw" / "a.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"%PDF")

    assert normalize_repo_path(target, project_root=root) == "data/raw/a.pdf"


def test_normalize_repo_path_keeps_external_absolute(tmp_path):
    root = tmp_path / "repo"
    external = tmp_path / "elsewhere" / "a.pdf"

    assert normalize_repo_path(external, project_root=root) == str(external)


def test_normalize_repo_path_makes_relative_posix():
    assert normalize_repo_path(r"data\papers\x\paper.md") == "data/papers/x/paper.md"


def test_resolve_stored_path_joins_relative(tmp_path):
    resolved = resolve_stored_path("data/raw/a.pdf", project_root=tmp_path)
    assert resolved == tmp_path / "data" / "raw" / "a.pdf"


def test_normalize_record_paths_only_known_fields(tmp_path):
    root = tmp_path / "repo"
    record = {
        "raw_pdf": root / "data" / "raw" / "a.pdf",
        "markdown_path": r"data\papers\a\paper.md",
        "title": r"not\a\path",
    }

    normalized = normalize_record_paths(record, project_root=root)

    assert normalized["raw_pdf"] == "data/raw/a.pdf"
    assert normalized["markdown_path"] == "data/papers/a/paper.md"
    assert normalized["title"] == r"not\a\path"
