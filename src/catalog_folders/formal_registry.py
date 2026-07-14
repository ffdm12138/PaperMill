from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from src.library.paper_number_ledger import PaperNumberLedger


@dataclass(frozen=True)
class FormalPaper:
    paper_number: str
    paper_name: str
    directory: Path
    catalog_path: Path
    metadata_path: Path


class FormalPaperRegistry:
    """Live, read-only formal identity view over the ledger and papers tree.

    Performs lightweight identity validation only — checks that the ledger
    entry, directory, marker, and catalog agree on paper_number and paper_name.
    Does NOT deep-validate freeze receipts, asset manifests, or catalog schemas
    (those belong to the formal library validation layer).
    """

    def __init__(self, *, papers_dir: Path, ledger: PaperNumberLedger):
        self.papers_dir = Path(papers_dir)
        self.ledger = ledger
        self._cache: tuple[FormalPaper, ...] | None = None

    def load(self, *, refresh: bool = False) -> tuple[FormalPaper, ...]:
        if self._cache is not None and not refresh:
            return self._cache
        active = {
            str(number): item for number, item in (self.ledger.load().get("items") or {}).items()
            if isinstance(item, dict) and item.get("state") == "active"
        }
        papers: list[FormalPaper] = []
        seen: set[str] = set()
        for number, item in sorted(active.items()):
            if len(number) != 16 or not number.isdigit():
                raise ValueError(f"invalid active paper_number: {number}")
            paper_name = str(item.get("paper_name") or "")
            folder_name = str(item.get("folder_name") or "")
            if not paper_name or folder_name != paper_name:
                raise ValueError(f"active ledger identity mismatch: {number}")
            if paper_name in seen:
                raise ValueError(f"duplicate active paper_name: {paper_name}")
            folder = self.papers_dir / paper_name
            if not folder.is_dir():
                raise ValueError(f"active formal directory missing: {folder}")

            # Lightweight identity check: marker + catalog agree on identities
            markers = sorted(folder.glob("*.paper.number"))
            if len(markers) != 1:
                raise ValueError(f"formal paper requires exactly one marker: {folder}")
            marker = json.loads(markers[0].read_text(encoding="utf-8"))
            if marker.get("paper_number") != number:
                raise ValueError(f"marker paper_number mismatch: {folder}")
            if marker.get("folder_name") != paper_name:
                raise ValueError(f"marker folder_name mismatch: {folder}")
            catalog_path = folder / f"{paper_name}.catalog.json"
            if not catalog_path.is_file():
                raise ValueError(f"catalog missing: {catalog_path}")
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            if catalog.get("paper_number") != number:
                raise ValueError(f"catalog paper_number mismatch: {folder}")
            if catalog.get("paper_name") != paper_name:
                raise ValueError(f"catalog paper_name mismatch: {folder}")

            seen.add(paper_name)
            papers.append(FormalPaper(
                paper_number=number, paper_name=paper_name, directory=folder,
                catalog_path=catalog_path,
                metadata_path=folder / f"{paper_name}.metadata.json",
            ))

        actual = {
            child.name for child in self.papers_dir.iterdir()
            if child.is_dir() and not child.name.startswith(".")
        } if self.papers_dir.is_dir() else set()
        orphans = sorted(actual - seen)
        if orphans:
            raise ValueError(f"orphan formal directories: {', '.join(orphans)}")
        self._cache = tuple(papers)
        return self._cache

    def resolve(self, identity: str) -> FormalPaper | None:
        return next((paper for paper in self.load() if identity in {paper.paper_number, paper.paper_name}), None)
