"""Read-only v2 catalog access backed by data/catalog/all.catalog.json.

After catalog/metadata separation, all.catalog entries are content-only (no
metadata). Bibliographic facts for the compact view are loaded from
data/papers/<pid>/...metadata.json via PaperLibrary.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config.settings import ALL_CATALOG_PATH, PAPERS_DIR
from src.services.paper_library import PaperLibrary
from src.services.v2_library import AllCatalogBuilder


def _read_json(path: Path, default: dict | None = None) -> dict:
    if not path.exists():
        return default or {}
    return json.loads(path.read_text(encoding="utf-8"))


def _fmt_score(value: Any) -> str:
    return str(value) if value not in (None, "") else "-"


def _authors_short(metadata: dict) -> str:
    authors = metadata.get("authors") or []
    if not authors:
        return ""
    first = authors[0]
    fam = ""
    if isinstance(first, dict):
        fam = first.get("family") or first.get("full_name") or ""
    else:
        fam = str(first)
    fam = str(fam).split(",")[0].strip()
    if not fam:
        return ""
    return f"{fam} et al." if len(authors) > 1 else fam


def build_compact_catalog_text(
    papers: list[dict],
    library: PaperLibrary | None = None,
    *,
    include_metadata: bool = False,
) -> str:
    """Compact inventory text.

    `papers` are all.catalog entries (content only). This is a **display-layer
    view only** — it never writes metadata into all.catalog storage.

    - ``include_metadata=False`` (default): strict content-only. Only catalog
      content (content_title, classification, screening, research_card, ...) is
      shown. Use this for catalog-planning prompts so the model picks papers by
      content, not by bibliography.
    - ``include_metadata=True``: additionally join bibliographic bits
      (year/authors/venue/doi/canonical title) loaded from metadata via
      `library` (PaperLibrary). Use this only when the consumer needs citation
      context (e.g. BibTeX/export flows). Requires `library`.
    """
    lines = ["# 文献目录（紧凑视图）", ""]
    for entry in papers:
        pid = entry.get("paper_id", "")
        number = entry.get("paper_number", "")
        catalog = entry  # all.catalog entry IS the flat content entry
        content_identity = catalog.get("content_identity") or {}
        classification = catalog.get("classification") or {}
        screening = catalog.get("screening") or {}
        card = catalog.get("research_card") or {}
        notes = catalog.get("content_notes") or {}
        title = content_identity.get("content_title") or ""
        metadata = library.load_metadata(number) if (include_metadata and library) else None
        meta_bits = []
        if metadata:
            year = metadata.get("year")
            if year:
                meta_bits.append(str(year))
            ashort = _authors_short(metadata)
            if ashort:
                meta_bits.append(ashort)
            venue = ((metadata.get("container") or {}).get("journal")
                     or (metadata.get("container") or {}).get("conference")
                     or "")
            if venue:
                meta_bits.append(venue)
            doi = ((metadata.get("identifiers") or {}).get("doi") or "")
            if doi:
                meta_bits.append(doi)
            if not title:
                title = (metadata.get("title") or {}).get("original") or ""
        domain_bits = []
        if classification.get("primary_domain"):
            domain_bits.append(str(classification["primary_domain"]))
        topics = classification.get("topic_tags") or []
        if topics:
            domain_bits.append(",".join(topics))
        domain = " / ".join(domain_bits)
        read_decision = screening.get("read_decision") or ""
        relevance = _fmt_score(screening.get("relevance_score"))
        priority = _fmt_score(screening.get("reading_priority") or screening.get("method_quality_score"))
        one_sentence = notes.get("short_summary") or card.get("research_problem") or "(未总结)"
        method = card.get("method_summary") or ""
        conclusion = " ".join(card.get("main_findings") or [])[:80]
        usefulness = card.get("usefulness_for_user") or ""
        best_for = ",".join(notes.get("possible_use_in_writing") or [])
        lines.append(f"- [{priority}] {number} {pid} {title}")
        if meta_bits:
            lines.append(f"  meta: {' | '.join(meta_bits)}")
        if domain:
            lines.append(f"  domain: {domain}")
        decision_bits = []
        if read_decision:
            decision_bits.append(f"read={read_decision}")
        decision_bits.append(f"relevance={relevance}")
        decision_bits.append(f"priority={priority}")
        lines.append(f"  screening: {' '.join(decision_bits)}")
        lines.append(f"  summary: {one_sentence}")
        if method:
            lines.append(f"  method: {method}")
        if conclusion:
            lines.append(f"  conclusion: {conclusion}")
        if usefulness:
            lines.append(f"  usefulness: {usefulness}")
        if best_for:
            lines.append(f"  best_for_sections: {best_for}")
    return "\n".join(lines)


class Catalog:
    """Small compatibility wrapper over the v2 all-catalog file."""

    def __init__(self, path: Path = ALL_CATALOG_PATH, papers_dir: Path = PAPERS_DIR):
        self.path = Path(path)
        self.papers_dir = papers_dir
        self.library = PaperLibrary(all_catalog_path=self.path, papers_dir=papers_dir)

    @staticmethod
    def _empty_data() -> dict:
        return {"schema_version": "2.0", "papers": []}

    def load(self) -> dict:
        # Read-only: if all.catalog is absent, build an in-memory snapshot WITHOUT
        # writing to disk (no ledger / marker / per-paper catalog writes). Disk
        # writes belong to `rebuild_all_catalog.py --apply`, commit, or an
        # explicit `?rebuild=true` API call.
        if not self.path.exists():
            return AllCatalogBuilder(all_catalog_path=self.path, papers_dir=self.papers_dir).build_readonly_snapshot()
        return _read_json(self.path, self._empty_data())

    def list_papers(self) -> list[dict]:
        return list(self.load().get("papers", []))

    def get(self, paper_id_or_number: str) -> dict | None:
        for item in self.list_papers():
            if item.get("paper_id") == paper_id_or_number or item.get("paper_number") == paper_id_or_number:
                return item
        return None

    def validate(self) -> list[str]:
        errors: list[str] = []
        data = self.load()
        if not isinstance(data.get("papers"), list):
            return ["all catalog missing papers list"]
        seen_numbers: set[str] = set()
        seen_ids: set[str] = set()
        for i, item in enumerate(data["papers"]):
            ctx = f"papers[{i}]"
            for key in ("paper_number", "paper_id"):
                if key not in item:
                    errors.append(f"{ctx} missing {key}")
            number = item.get("paper_number")
            pid = item.get("paper_id")
            if number in seen_numbers:
                errors.append(f"{ctx} duplicate paper_number {number}")
            if pid in seen_ids:
                errors.append(f"{ctx} duplicate paper_id {pid}")
            seen_numbers.add(number)
            seen_ids.add(pid)
        return errors

    def build_compact_catalog(
        self,
        topics: list[str] | None = None,
        *,
        include_metadata: bool = False,
    ) -> str:
        papers = self.list_papers()
        if topics:
            wanted = set(topics)
            def _topics_of(p):
                cls = (p.get("classification") or {})
                return set(cls.get("topic_tags") or cls.get("topics") or [])
            papers = [p for p in papers if wanted & _topics_of(p)]
        return build_compact_catalog_text(
            papers, library=self.library, include_metadata=include_metadata
        )

    def compact_items(self, topics: list[str] | None = None) -> list[dict[str, Any]]:
        papers = self.list_papers()
        if topics:
            wanted = set(topics)
            def _topics_of(p):
                cls = (p.get("classification") or {})
                return set(cls.get("topic_tags") or cls.get("topics") or [])
            papers = [p for p in papers if wanted & _topics_of(p)]
        return papers
