from __future__ import annotations

import json
from pathlib import Path

from filelock import FileLock

from src.catalog_folders.assignment import load_assignment, valid_decisions
from src.catalog_folders.formal_registry import FormalPaperRegistry
from src.catalog_folders.link_backend import create_paper_link, inspect_paper_link, remove_paper_link
from src.catalog_folders.models import Category
from src.catalog_folders.registry import load_registry
from src.utils.atomic_io import atomic_write_json


def _categories(registry_path: Path) -> list[Category]:
    rows = load_registry(registry_path)["categories"] if registry_path.is_file() else []
    return [Category(
        category_id=row["category_id"], keyword_zh=row["keyword_zh"],
        normalized_keyword_zh=row["normalized_keyword_zh"], directory_name=row["directory_name"],
        source_notebook=row["source_notebook"], definition_sha256=row["definition_sha256"],
        classification_enabled=bool(row.get("classification_enabled", True)), retired_at=row.get("retired_at"),
        guidance_zh=row.get("guidance_zh"), aliases_zh=tuple(row.get("aliases_zh") or ()),
        exclusions_zh=tuple(row.get("exclusions_zh") or ()),
    ) for row in rows if row.get("classification_enabled", True) and not row.get("retired_at")]


def _sync_members(directory: Path, wanted: dict[str, Path], *, apply: bool) -> dict:
    existing: dict[str, Path] = {}
    unmanaged: list[str] = []
    if directory.is_dir():
        for child in directory.iterdir():
            if child.name == ".category.json":
                continue
            link = inspect_paper_link(child)
            if link is None or len(child.name) != 16 or not child.name.isdigit():
                unmanaged.append(str(child))
            else:
                existing[child.name] = link.target
    if unmanaged:
        raise ValueError(f"unmanaged category paths: {', '.join(unmanaged)}")
    remove = sorted(set(existing) - set(wanted) | {number for number in existing.keys() & wanted.keys() if existing[number] != wanted[number].resolve()})
    add = sorted(set(wanted) - set(existing) | {number for number in existing.keys() & wanted.keys() if existing[number] != wanted[number].resolve()})
    if apply:
        directory.mkdir(parents=True, exist_ok=True)
        for number in remove:
            remove_paper_link(directory / number)
        for number in add:
            create_paper_link(directory / number, wanted[number])
    return {"added": add, "removed": remove}


def reconcile_catalog_folders(*, root: Path, formal_registry: FormalPaperRegistry, apply: bool) -> dict:
    root = Path(root)
    state = root / ".state"
    lock = FileLock(str(state / "category.lock"))
    with lock:
        state.mkdir(parents=True, exist_ok=True)
        dirty = state / "DIRTY"
        if apply:
            dirty.write_text("reconcile in progress\n", encoding="utf-8")
        try:
            papers = formal_registry.load(refresh=True)
            categories = _categories(state / "category_registry.json")
            targets = {paper.paper_number: paper.directory for paper in papers}
            report = {"all": _sync_members(root / "all", targets, apply=apply), "categories": {}, "pending": {}}
            pending: dict[str, Path] = {}
            category_wanted = {category.category_id: {} for category in categories}
            for paper in papers:
                assignment = load_assignment(state / "assignments" / f"{paper.paper_number}.json")
                decisions = valid_decisions(assignment, paper, categories)
                if len(decisions) != len(categories):
                    pending[paper.paper_number] = paper.directory
                for category in categories:
                    if decisions.get(category.category_id, {}).get("matched") is True:
                        category_wanted[category.category_id][paper.paper_number] = paper.directory
            for category in categories:
                folder = root / category.directory_name
                if apply:
                    folder.mkdir(parents=True, exist_ok=True)
                    atomic_write_json(folder / ".category.json", category.to_dict(), indent=2)
                report["categories"][category.category_id] = _sync_members(folder, category_wanted[category.category_id], apply=apply)
            report["pending"] = _sync_members(root / "_pending", pending, apply=apply)
            report.update({"formal_papers": len(papers), "category_count": len(categories), "pending_count": len(pending)})
            if apply:
                dirty.unlink(missing_ok=True)
            return report
        except Exception:
            raise
