from __future__ import annotations

import json
from pathlib import Path

from src.catalog_folders.link_backend import inspect_paper_link


SYSTEM_DIRS = {"all", "_pending", ".state"}


class CatalogFolderReader:
    def __init__(self, *, root: Path, papers_dir: Path):
        self.root = Path(root); self.papers_dir = Path(papers_dir)

    def _resolve_category(self, name: str) -> Path:
        if name == "_pending":
            raise ValueError("_pending is not a reliable writer category")
        direct = self.root / name
        if direct.is_dir() and (name == "all" or name not in SYSTEM_DIRS):
            return direct
        for path in list_categories(self.root):
            try: data=json.loads((path/".category.json").read_text(encoding="utf-8"))
            except Exception: continue
            if name in {data.get("category_id"), data.get("keyword_zh"), data.get("directory_name")}:
                return path
        raise FileNotFoundError(f"catalog category not found: {name}")

    def list_papers(self, categories: list[str] | None = None, *, mode: str = "union") -> list[dict]:
        if (self.root / ".state" / "DIRTY").exists():
            raise RuntimeError("catalog folder state is DIRTY")
        names = categories or ["all"]
        sets = []
        rows_by_number: dict[str, dict] = {}
        for name in names:
            rows=read_category_members(self._resolve_category(name),papers_dir=self.papers_dir)
            numbers={row["paper_number"] for row in rows}; sets.append(numbers)
            rows_by_number.update({row["paper_number"]:{**row["catalog"],"formal_directory":str(row["directory"])} for row in rows})
        selected = set.intersection(*sets) if mode == "intersection" and sets else set.union(*sets) if sets else set()
        return [rows_by_number[number] for number in sorted(selected)]

    def get(self, identity: str) -> dict | None:
        return next((row for row in self.list_papers(["all"]) if identity in {row.get("paper_number"),row.get("paper_id")}),None)

    def compact_batches(self, categories: list[str] | None = None, *, mode: str = "union", batch_size: int = 15):
        if not 10 <= batch_size <= 20:
            raise ValueError("writer Catalog batch_size must be between 10 and 20")
        papers=self.list_papers(categories,mode=mode)
        for offset in range(0,len(papers),batch_size):
            yield papers[offset:offset+batch_size]

    def validate(self) -> list[str]:
        errors: list[str] = []
        if (self.root / ".state" / "DIRTY").exists():
            errors.append("catalog folder state is DIRTY")
        for category in [self.root / "all", *list_categories(self.root)]:
            try:
                read_category_members(category, papers_dir=self.papers_dir)
            except Exception as exc:
                errors.append(str(exc))
        return errors


def list_categories(root: Path) -> list[Path]:
    return sorted(path for path in Path(root).iterdir() if path.is_dir() and path.name not in SYSTEM_DIRS and not path.name.startswith("."))


def read_category_members(category_dir: Path, *, papers_dir: Path) -> list[dict]:
    papers_root = Path(papers_dir).resolve()
    result: list[dict] = []
    for path in sorted(Path(category_dir).iterdir()):
        if path.name == ".category.json":
            continue
        if len(path.name) != 16 or not path.name.isdigit():
            raise ValueError(f"unmanaged category member: {path}")
        link = inspect_paper_link(path)
        if link is None or papers_root not in link.target.parents:
            raise ValueError(f"invalid category link: {path}")
        paper_id = link.target.name
        catalog_path = link.target / f"{paper_id}.catalog.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        if catalog.get("paper_number") != path.name or catalog.get("paper_id") != paper_id:
            raise ValueError(f"category link identity mismatch: {path}")
        result.append({"paper_number": path.name, "paper_id": paper_id, "directory": link.target, "catalog": catalog})
    return result
