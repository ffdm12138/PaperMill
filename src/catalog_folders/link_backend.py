from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PaperLink:
    path: Path
    target: Path
    kind: str


def _is_junction(path: Path) -> bool:
    checker = getattr(os.path, "isjunction", None)
    if checker is not None:
        return bool(checker(path))
    try:
        return bool(os.lstat(path).st_file_attributes & 0x400)
    except (AttributeError, OSError):
        return False


def inspect_paper_link(link_path: Path) -> PaperLink | None:
    path = Path(link_path)
    if path.is_symlink():
        raw = Path(os.readlink(path))
        target = (path.parent / raw).resolve() if not raw.is_absolute() else raw.resolve()
        return PaperLink(path, target, "symlink")
    if os.name == "nt" and _is_junction(path):
        try:
            raw = Path(os.readlink(path))
            raw_text = str(raw)
            if raw_text.startswith("\\\\?\\"):
                raw = Path(raw_text[4:])
            target = raw.resolve()
        except OSError:
            target = path.resolve()
        return PaperLink(path, target, "junction")
    return None


def create_paper_link(link_path: Path, target_path: Path) -> PaperLink:
    link = Path(link_path)
    target = Path(target_path).resolve(strict=True)
    if not target.is_dir():
        raise ValueError(f"paper link target is not a directory: {target}")
    current = inspect_paper_link(link)
    if current:
        if current.target == target:
            return current
        raise FileExistsError(f"paper link has a different target: {link}")
    if link.exists():
        raise FileExistsError(f"paper link path is not a managed link: {link}")
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if completed.returncode:
            raise OSError(f"junction creation failed: {completed.stderr or completed.stdout}")
    else:
        os.symlink(os.path.relpath(target, link.parent), link, target_is_directory=True)
    inspected = inspect_paper_link(link)
    if inspected is None or inspected.target != target:
        raise OSError(f"created paper link failed verification: {link}")
    return inspected


def remove_paper_link(link_path: Path) -> None:
    path = Path(link_path)
    link = inspect_paper_link(path)
    if link is None:
        if path.exists():
            raise ValueError(f"refusing to remove unmanaged path: {path}")
        return
    if link.kind == "junction":
        os.rmdir(path)
    else:
        path.unlink()
