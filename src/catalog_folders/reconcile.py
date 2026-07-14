from __future__ import annotations

import json
from pathlib import Path

from filelock import FileLock

from src.catalog_folders.assignment import load_assignment, valid_decisions
from src.catalog_folders.formal_registry import FormalPaperRegistry
from src.catalog_folders.link_backend import create_paper_link, inspect_paper_link, remove_paper_link
from src.catalog_folders.models import Category
from src.catalog_folders.registry import load_categories
from src.catalog_folders.registry_schema import validate_registry_entry
from src.utils.atomic_io import atomic_write_json


# ── Full-directory reconcile (authoritative, for bulk rebuild) ────────

def _sync_members(directory: Path, wanted: dict[str, Path], *, apply: bool) -> dict:
    """Replace all members of *directory* with exactly the *wanted* set.

    This is the authoritative bulk operation — it enumerates every existing
    member and removes any paper not present in *wanted*.  Callers must pass
    the complete desired membership; partial wanted sets will destroy other
    papers' links.

    Keys in *wanted* are ``paper_name`` — the human-readable paper identity
    that also serves as the link name in the category directory.
    """
    existing: dict[str, Path] = {}
    unmanaged: list[str] = []
    if directory.is_dir():
        for child in directory.iterdir():
            if child.name == ".category.json":
                continue
            link = inspect_paper_link(child)
            if link is None:
                unmanaged.append(str(child))
            else:
                existing[child.name] = link.target
    if unmanaged:
        raise ValueError(f"unmanaged category paths: {', '.join(unmanaged)}")
    remove = sorted(
        set(existing) - set(wanted)
        | {name for name in existing.keys() & wanted.keys()
           if existing[name] != wanted[name].resolve()}
    )
    add = sorted(
        set(wanted) - set(existing)
        | {name for name in existing.keys() & wanted.keys()
           if existing[name] != wanted[name].resolve()}
    )
    if apply:
        directory.mkdir(parents=True, exist_ok=True)
        for name in remove:
            remove_paper_link(directory / name)
        for name in add:
            create_paper_link(directory / name, wanted[name])
    return {"added": add, "removed": remove}


def _retire_unknown_category_directories(
    root: Path,
    categories: list[Category],
    *,
    apply: bool,
) -> dict[str, dict]:
    """Remove category directories no longer present in the active Registry.

    Only directories carrying a valid controlled ``.category.json`` marker are
    eligible.  Unknown or unmanaged content fails closed instead of being
    deleted.  This keeps notebook deletion from leaving ghost categories while
    preserving the repository's controlled-link boundary.
    """
    active_names = {category.directory_name for category in categories}
    reports: dict[str, dict] = {}
    for directory in sorted(root.iterdir() if root.is_dir() else []):
        if not directory.is_dir():
            continue
        if directory.name in {"all", "_pending", ".state"} or directory.name.startswith("."):
            continue
        if directory.name in active_names:
            continue
        marker = directory / ".category.json"
        if not marker.is_file():
            raise ValueError(f"unknown unmanaged category directory: {directory}")
        try:
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            validated = validate_registry_entry(
                marker_data,
                require_sha256_match=False,
            )
        except Exception as exc:
            raise ValueError(f"invalid controlled category marker: {marker}: {exc}") from exc
        if validated["directory_name"] != directory.name:
            raise ValueError(
                f"category marker directory mismatch: {marker}: "
                f"{validated['directory_name']!r} != {directory.name!r}"
            )
        member_report = _sync_members(directory, {}, apply=apply)
        reports[directory.name] = member_report
        if apply:
            marker.unlink()
            directory.rmdir()
    return reports


# ── Single-member reconcile (safe for per-paper updates) ──────────────

def reconcile_one_member(
    *,
    directory: Path,
    paper_name: str,
    target: Path,
    should_exist: bool,
    apply: bool,
) -> dict:
    """Create or remove a single paper's link in *directory*.

    Only operates on *paper_name* — other members in the directory are
    never enumerated, removed, or modified.

    Returns ``{"action": "added"|"removed"|"unchanged"}``.
    """
    directory = Path(directory)
    link_path = directory / paper_name
    current = inspect_paper_link(link_path)

    if should_exist:
        if current is not None and current.target == target.resolve():
            return {"action": "unchanged", "paper_name": paper_name}
        if apply:
            if current is not None:
                remove_paper_link(link_path)
            directory.mkdir(parents=True, exist_ok=True)
            create_paper_link(link_path, target)
        return {"action": "added", "paper_name": paper_name}
    else:
        if current is None:
            return {"action": "unchanged", "paper_name": paper_name}
        if apply:
            remove_paper_link(link_path)
        return {"action": "removed", "paper_name": paper_name}


# ── Per-paper membership reconcile ────────────────────────────────────

def reconcile_paper_membership(
    *,
    paper: object,  # FormalPaper
    assignment: dict | None,
    categories: list[Category],
    root: Path,
    apply: bool,
) -> dict:
    """Reconcile a single paper's category links atomically.

    Must be called inside a per-paper lock.  Creates or removes **only**
    the links for *paper* — other papers' links are never touched.

    Link names are ``paper_name`` (human-readable identity).
    """
    root = Path(root)
    decisions = valid_decisions(assignment, paper, categories) if assignment else {}
    report: dict[str, list[str]] = {"added": [], "removed": []}

    # all/ link
    all_result = reconcile_one_member(
        directory=root / "all",
        paper_name=paper.paper_name,
        target=paper.directory,
        should_exist=True,
        apply=apply,
    )
    if all_result["action"] == "added":
        report["added"].append("all")
    elif all_result["action"] == "removed":
        report["removed"].append("all")

    # category links
    for category in categories:
        folder = root / category.directory_name
        if apply:
            folder.mkdir(parents=True, exist_ok=True)
            atomic_write_json(folder / ".category.json", category.to_dict(), indent=2)
        matched = decisions.get(category.category_id, {}).get("matched") is True
        cat_result = reconcile_one_member(
            directory=folder,
            paper_name=paper.paper_name,
            target=paper.directory,
            should_exist=matched,
            apply=apply,
        )
        if cat_result["action"] == "added":
            report["added"].append(category.category_id)
        elif cat_result["action"] == "removed":
            report["removed"].append(category.category_id)

    # _pending
    complete = len(decisions) == len(categories) and len(categories) > 0
    pending_result = reconcile_one_member(
        directory=root / "_pending",
        paper_name=paper.paper_name,
        target=paper.directory,
        should_exist=not complete,
        apply=apply,
    )
    if pending_result["action"] == "added":
        report["added"].append("_pending")
    elif pending_result["action"] == "removed":
        report["removed"].append("_pending")

    return report


# ── Full catalog-folder reconcile ─────────────────────────────────────

def reconcile_catalog_folders(*, root: Path, formal_registry: FormalPaperRegistry, apply: bool,
                               allow_empty_categories: bool = False) -> dict:
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
            categories = load_categories(state / "category_registry.json")

            if len(papers) > 0 and len(categories) == 0 and not allow_empty_categories:
                raise ValueError(
                    f"classification configuration invalid: {len(papers)} formal papers "
                    f"but 0 active categories; run sync_catalog_categories.py or pass "
                    f"--allow-empty-categories"
                )

            targets = {paper.paper_name: paper.directory for paper in papers}
            report = {
                "all": _sync_members(root / "all", targets, apply=apply),
                "categories": {},
                "pending": {},
                "retired_categories": _retire_unknown_category_directories(
                    root,
                    categories,
                    apply=apply,
                ),
            }
            pending: dict[str, Path] = {}
            category_wanted = {category.category_id: {} for category in categories}
            for paper in papers:
                assignment = load_assignment(state / "assignments" / f"{paper.paper_number}.json")
                decisions = valid_decisions(assignment, paper, categories)
                if len(decisions) != len(categories):
                    pending[paper.paper_name] = paper.directory
                for category in categories:
                    if decisions.get(category.category_id, {}).get("matched") is True:
                        category_wanted[category.category_id][paper.paper_name] = paper.directory
            for category in categories:
                folder = root / category.directory_name
                if apply:
                    folder.mkdir(parents=True, exist_ok=True)
                    atomic_write_json(folder / ".category.json", category.to_dict(), indent=2)
                report["categories"][category.category_id] = _sync_members(
                    folder, category_wanted[category.category_id], apply=apply,
                )
            report["pending"] = _sync_members(root / "_pending", pending, apply=apply)
            report.update({
                "formal_papers": len(papers),
                "category_count": len(categories),
                "pending_count": len(pending),
            })
            if apply:
                dirty.unlink(missing_ok=True)
            return report
        except Exception:
            raise
