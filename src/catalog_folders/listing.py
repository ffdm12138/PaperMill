"""Leaf category-listing readers (no doctor/validation dependencies).

Extracted from ``reader`` so ``validation`` and ``reader`` no longer form a
late-import cycle: validation imports listing; reader imports validation.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from src.catalog_folders.link_backend import inspect_paper_link

SYSTEM_DIRS = {"all", "_pending", ".state"}
_PAPER_NUMBER_RE = re.compile(r"^\d{16}$")


def list_categories(root: Path) -> list[Path]:
    return sorted(
        path for path in Path(root).iterdir()
        if path.is_dir() and path.name not in SYSTEM_DIRS and not path.name.startswith(".")
    )



def read_category_members(category_dir: Path, *, papers_dir: Path) -> list[dict]:
    """Read all paper links in a category directory.

    Each member must be a managed junction/symlink whose target is under
    ``papers_dir`` and contains valid catalog with matching identities.
    Duplicate ``paper_number`` or ``paper_name`` values within the same
    category are rejected.
    """
    papers_root = Path(papers_dir).resolve()
    result: list[dict] = []
    seen_numbers: dict[str, str] = {}  # paper_number → link_name
    seen_names: dict[str, str] = {}    # paper_name → paper_number
    for path in sorted(Path(category_dir).iterdir()):
        if path.name == ".category.json":
            continue
        link = inspect_paper_link(path)
        if link is None:
            raise ValueError(f"unmanaged category member (not a valid link): {path}")
        if path.name != link.target.name:
            raise ValueError(f"category link name mismatch: {path} -> {link.target}")
        if _PAPER_NUMBER_RE.match(path.name):
            raise ValueError(f"old number-named link: {path}")
        if link.target.parent != papers_root:
            raise ValueError(f"category link target escapes papers dir: {path}")
        paper_name = link.target.name
        catalog_path = link.target / f"{paper_name}.catalog.json"
        if not catalog_path.is_file():
            raise ValueError(f"category link target missing catalog: {path}")
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        # Verify paper_name matches
        if catalog.get("paper_name") != paper_name:
            raise ValueError(f"category link identity mismatch: {path}")
        # Verify paper_number validity
        paper_number = str(catalog.get("paper_number") or "")
        if not paper_number:
            raise ValueError(f"category link target missing paper_number in catalog: {path}")
        if not _PAPER_NUMBER_RE.match(paper_number):
            raise ValueError(
                f"category link target has invalid paper_number "
                f"({paper_number!r}) in catalog: {path}"
            )
        if not catalog.get("paper_name"):
            raise ValueError(f"category link target missing paper_name in catalog: {path}")
        # Verify marker matches catalog
        markers = sorted(link.target.glob("*.paper.number"))
        if len(markers) == 1:
            import json as _json
            marker = _json.loads(markers[0].read_text(encoding="utf-8"))
            if marker.get("paper_number") != paper_number:
                raise ValueError(
                    f"marker paper_number ({marker.get('paper_number')}) "
                    f"!= catalog paper_number ({paper_number}): {path}"
                )
        # Detect duplicates
        if paper_number in seen_numbers:
            raise ValueError(
                f"duplicate paper_number {paper_number} in category: "
                f"{seen_numbers[paper_number]} and {path.name}"
            )
        if paper_name in seen_names:
            raise ValueError(
                f"duplicate paper_name {paper_name} in category: "
                f"paper_numbers {seen_names[paper_name]} and {paper_number}"
            )
        seen_numbers[paper_number] = path.name
        seen_names[paper_name] = paper_number
        result.append({
            "paper_number": paper_number,
            "paper_name": paper_name,
            "directory": link.target,
            "catalog": catalog,
        })
    return result


