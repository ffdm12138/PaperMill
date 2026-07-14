"""Read formal paper assets through the live ledger-backed registry."""
from __future__ import annotations

import json
from pathlib import Path

from config.settings import PAPERS_DIR, PAPER_NUMBER_LEDGER_PATH
from src.catalog_folders.formal_registry import FormalPaperRegistry
from src.library.paper_number_ledger import PaperNumberLedger
from src.naming import safe_child, validate_image_name


class PaperLibrary:
    def __init__(self, *, papers_dir: str | Path = PAPERS_DIR, ledger_path: str | Path = PAPER_NUMBER_LEDGER_PATH):
        self.papers_dir = Path(papers_dir)
        self.registry = FormalPaperRegistry(papers_dir=self.papers_dir, ledger=PaperNumberLedger(ledger_path))

    def resolve(self, identity: str) -> dict | None:
        paper = self.registry.resolve(identity)
        if paper is None:
            return None
        return {"paper_number": paper.paper_number, "paper_name": paper.paper_name, "directory": str(paper.directory), "catalog_path": str(paper.catalog_path), "metadata_path": str(paper.metadata_path)}

    def _paper(self, identity: str):
        paper = self.registry.resolve(identity)
        if paper is None:
            raise FileNotFoundError(f"paper not found: {identity}")
        return paper

    def paper_dir(self, identity: str) -> Path: return self._paper(identity).directory
    def metadata_path(self, identity: str) -> Path: return self._paper(identity).metadata_path
    def catalog_path(self, identity: str) -> Path: return self._paper(identity).catalog_path
    def markdown_path(self, identity: str) -> Path:
        paper=self._paper(identity); return paper.directory/f"{paper.paper_name}.md"
    def pdf_path(self, identity: str) -> Path:
        paper=self._paper(identity); return paper.directory/f"{paper.paper_name}.pdf"
    def images_dir(self, identity: str) -> Path: return self._paper(identity).directory/"images"
    def load_metadata(self, identity: str) -> dict | None: return json.loads(self.metadata_path(identity).read_text(encoding="utf-8"))
    def load_catalog(self, identity: str) -> dict | None: return json.loads(self.catalog_path(identity).read_text(encoding="utf-8"))
    def catalog_entry(self, identity: str) -> dict | None:
        try: return self.load_catalog(identity)
        except FileNotFoundError: return None
    def read_markdown(self, identity: str, max_chars: int | None = None) -> str | None:
        try: text=self.markdown_path(identity).read_text(encoding="utf-8",errors="ignore")
        except FileNotFoundError: return None
        return text[:max_chars] if max_chars else text
    def list_images(self, identity: str) -> list[str]:
        folder=self.images_dir(identity); return sorted(path.name for path in folder.iterdir() if path.is_file()) if folder.is_dir() else []
    def image_path(self, identity: str, image_name: str) -> Path:
        validate_image_name(image_name); return safe_child(self.images_dir(identity), image_name)
    def read_multiple(self, identities: list[str], max_chars_each: int | None = None) -> dict[str,str]:
        return {identity:text for identity in identities if (text:=self.read_markdown(identity,max_chars_each)) is not None}
    def all_paper_numbers(self) -> list[str]: return [paper.paper_number for paper in self.registry.load()]
    def all_entries(self) -> list[dict]: return [self.resolve(paper.paper_number) for paper in self.registry.load()]
    def metadata_for_all(self) -> dict[str,dict]: return {paper.paper_number:self.load_metadata(paper.paper_number) for paper in self.registry.load()}
