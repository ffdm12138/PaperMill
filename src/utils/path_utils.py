"""Path helpers for persisted repository paths.

JSON facts should store repo-relative POSIX paths whenever a file lives inside
the project root. Absolute paths outside the repo are preserved so callers can
surface an audit warning instead of silently rewriting user data.
"""
from __future__ import annotations

import re
from pathlib import Path, PureWindowsPath
from typing import Iterable

from config.settings import PROJECT_ROOT


_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


def is_windows_abs_path(path: str) -> bool:
    """Return True for drive-letter Windows absolute paths."""
    return bool(path and _WINDOWS_ABS_RE.match(str(path)))


def _as_path(value: str | Path) -> Path:
    text = str(value)
    if is_windows_abs_path(text):
        return Path(PureWindowsPath(text))
    return Path(text)


def _relative_to_project(path: Path, project_root: Path) -> str | None:
    try:
        rel = path.resolve().relative_to(project_root.resolve())
        return rel.as_posix()
    except (OSError, ValueError):
        return None


def _normalize_relative_text(text: str) -> str:
    return text.replace("\\", "/").lstrip("./")


def _windows_repo_relative(text: str, project_root: Path) -> str | None:
    win_text = text.replace("\\", "/")
    root_text = str(project_root).replace("\\", "/").rstrip("/")
    if win_text.lower().startswith(root_text.lower() + "/"):
        return win_text[len(root_text) + 1:]
    for marker in ("/data/raw/", "/data/papers/", "/data/catalog/", "/data/manifests/"):
        idx = win_text.lower().find(marker)
        if idx >= 0:
            return win_text[idx + 1:]
    return None


def normalize_repo_path(path: str | Path, project_root: Path = PROJECT_ROOT) -> str:
    """Normalize a stored path to repo-relative POSIX form when safe.

    - Relative paths are normalized to POSIX separators.
    - Absolute paths under ``project_root`` become repo-relative POSIX paths.
    - Absolute paths outside ``project_root`` are preserved.
    """
    if path is None:
        return ""
    text = str(path).strip()
    if not text:
        return ""

    if is_windows_abs_path(text):
        rel = _windows_repo_relative(text, Path(project_root))
        if rel is not None:
            return rel

    p = _as_path(text)
    if p.is_absolute():
        rel = _relative_to_project(p, Path(project_root))
        return rel if rel is not None else text
    return _normalize_relative_text(text)


def resolve_stored_path(path: str | Path, project_root: Path = PROJECT_ROOT) -> Path:
    """Resolve a stored repo-relative or absolute path for local IO."""
    if path is None:
        return Path(project_root)
    text = str(path).strip()
    if not text:
        return Path(project_root)
    p = _as_path(text)
    if p.is_absolute():
        return p
    return Path(project_root) / p


def normalize_record_paths(
    record: dict,
    fields: Iterable[str] = ("raw_pdf", "markdown", "markdown_path", "images_dir"),
    project_root: Path = PROJECT_ROOT,
) -> dict:
    """Return a shallow copy with known path fields normalized."""
    out = dict(record)
    for field in fields:
        if field in out and out[field]:
            out[field] = normalize_repo_path(out[field], project_root=project_root)
    return out
