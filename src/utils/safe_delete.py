"""Safe deletion helpers for confirmed duplicate import artifacts."""
from __future__ import annotations

import shutil
import os
from pathlib import Path


class SafeDeleteError(ValueError):
    pass


def _assert_inside_data_root(target: Path, data_root: Path) -> Path:
    target_abs = Path(os.path.abspath(target))
    root_abs = Path(os.path.abspath(data_root))
    if target_abs == root_abs:
        raise SafeDeleteError("refuse to delete data root itself")
    try:
        relative = target_abs.relative_to(root_abs)
    except ValueError as exc:
        raise SafeDeleteError(f"refuse to delete outside data root: {target}") from exc
    current = root_abs
    if current.is_symlink():
        raise SafeDeleteError(f"refuse symlink data root: {data_root}")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise SafeDeleteError(f"refuse to delete through symlink: {current}")
    resolved_root = root_abs.resolve(strict=False)
    resolved_target = target_abs.resolve(strict=False)
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        raise SafeDeleteError(f"refuse resolved path outside data root: {target}") from exc
    return target_abs


def safe_delete_duplicate_artifact(
    target: str | Path,
    *,
    data_root: str | Path,
    confirmed_duplicate: bool,
) -> dict:
    """Delete a duplicate artifact only after explicit duplicate confirmation."""
    if not confirmed_duplicate:
        raise SafeDeleteError("duplicate confirmation is required before deletion")
    target_path = _assert_inside_data_root(Path(target), Path(data_root))
    if target_path.is_symlink():
        raise SafeDeleteError(f"refuse to delete symlink: {target_path}")
    if not target_path.exists():
        return {"deleted": False, "path": str(target_path), "reason": "missing"}
    if target_path.is_dir():
        shutil.rmtree(target_path)
        return {"deleted": True, "path": str(target_path), "kind": "dir"}
    target_path.unlink()
    return {"deleted": True, "path": str(target_path), "kind": "file"}
