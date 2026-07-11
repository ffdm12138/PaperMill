from __future__ import annotations

from pathlib import Path

from src.catalog_folders.formal_registry import FormalPaperRegistry
from src.catalog_folders.reader import list_categories, read_category_members


def doctor(*, root: Path, formal_registry: FormalPaperRegistry) -> dict:
    root = Path(root); errors: list[str] = []
    try:
        papers = formal_registry.load(refresh=True)
    except Exception as exc:
        return {"writer_safe": False, "errors": [str(exc)], "dirty": (root / ".state" / "DIRTY").exists()}
    expected = {paper.paper_number for paper in papers}
    try:
        all_numbers = {row["paper_number"] for row in read_category_members(root / "all", papers_dir=formal_registry.papers_dir)}
    except Exception as exc:
        all_numbers = set(); errors.append(str(exc))
    if all_numbers != expected:
        errors.append(f"all membership mismatch: missing={sorted(expected-all_numbers)} extra={sorted(all_numbers-expected)}")
    category_count = 0
    try:
        for category in list_categories(root):
            read_category_members(category, papers_dir=formal_registry.papers_dir)
            category_count += 1
    except Exception as exc:
        errors.append(str(exc))
    dirty = (root / ".state" / "DIRTY").exists()
    if dirty:
        errors.append("catalog folder state is DIRTY")
    pending_count = 0
    try:
        pending_count = len(read_category_members(root / "_pending", papers_dir=formal_registry.papers_dir))
    except Exception as exc:
        errors.append(str(exc))
    return {"active_formal_papers": len(papers), "all_members": len(all_numbers), "pending": pending_count, "categories": category_count, "dirty": dirty, "writer_safe": not errors, "errors": errors}
