from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.library.paper_number_ledger import PaperNumberLedger
from src.library.validation import validate_formal_paper


@dataclass(frozen=True)
class FormalPaper:
    paper_number: str
    paper_id: str
    directory: Path
    catalog_path: Path
    metadata_path: Path


class FormalPaperRegistry:
    """Live, read-only formal identity view over the ledger and papers tree."""

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
        seen_ids: set[str] = set()
        for number, item in sorted(active.items()):
            if len(number) != 16 or not number.isdigit():
                raise ValueError(f"invalid active paper_number: {number}")
            paper_id = str(item.get("paper_id") or "")
            folder_name = str(item.get("folder_name") or "")
            if not paper_id or folder_name != paper_id:
                raise ValueError(f"active ledger identity mismatch: {number}")
            if paper_id in seen_ids:
                raise ValueError(f"duplicate active paper_id: {paper_id}")
            folder = self.papers_dir / paper_id
            if not folder.is_dir():
                raise ValueError(f"active formal directory missing: {number}")
            info = validate_formal_paper(folder, expected_paper_id=paper_id)
            if info["paper_number"] != number or info["paper_id"] != paper_id:
                raise ValueError(f"formal identity mismatch: {number}")
            seen_ids.add(paper_id)
            papers.append(FormalPaper(
                paper_number=number, paper_id=paper_id, directory=folder,
                catalog_path=folder / f"{paper_id}.catalog.json",
                metadata_path=folder / f"{paper_id}.metadata.json",
            ))
        actual = {
            child.name for child in self.papers_dir.iterdir()
            if child.is_dir() and not child.name.startswith(".")
        } if self.papers_dir.is_dir() else set()
        orphans = sorted(actual - seen_ids)
        if orphans:
            raise ValueError(f"orphan formal directories: {', '.join(orphans)}")
        self._cache = tuple(papers)
        return self._cache

    def resolve(self, identity: str) -> FormalPaper | None:
        return next((paper for paper in self.load() if identity in {paper.paper_number, paper.paper_id}), None)
