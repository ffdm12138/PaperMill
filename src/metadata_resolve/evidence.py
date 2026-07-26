"""Local-evidence extraction for the paper_raw metadata resolver.

Pulls trusted existing metadata, converted-Markdown front matter (header
region before any References heading), and a conservative PDF first-pages
title fallback. Reference-list DOIs are never local evidence for the paper
itself.
"""
from __future__ import annotations

import re
from pathlib import Path

from src.utils.identifiers import collect_dois_from_text
from src.metadata.schema import first_author_family, metadata_doi
from src.metadata_resolve.markdown_extract import (
    extract_front_matter_candidates_from_markdown,
    extract_metadata_from_markdown,
)
from src.metadata_resolve.names import surname as _surname


REFERENCES_HEADING_RE = re.compile(r"^\s{0,6}#{1,6}\s*(references|bibliography|参考文献)", re.IGNORECASE)
MD_HEADER_SCAN_CHARS = 15000
MD_HEADER_SCAN_LINES = 100


# ── Markdown DOI scope ─────────────────────────────────────────────────

def _split_header_and_references(md_text: str) -> tuple[str, str]:
    """Return (header_text, references_text). Header = everything before the
    first References/Bibliography/参考文献 heading."""
    lines = md_text.splitlines()
    boundary = len(lines)
    for idx, line in enumerate(lines):
        if REFERENCES_HEADING_RE.match(line):
            boundary = idx
            break
    header_lines = lines[:min(boundary, MD_HEADER_SCAN_LINES)]
    header_text = "\n".join(header_lines)
    # cap header to the scan window
    if len(header_text) > MD_HEADER_SCAN_CHARS:
        header_text = header_text[:MD_HEADER_SCAN_CHARS]
    references_text = "\n".join(lines[boundary:])
    return header_text, references_text


def _extract_pdf_first_pages_text(pdf_path: Path, max_pages: int = 3) -> str:
    """Best-effort text from the first PDF pages; used only as local evidence."""
    if not pdf_path.exists():
        return ""
    try:
        import fitz
    except ImportError:
        return ""
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return ""
    try:
        chunks: list[str] = []
        for page_num in range(min(max_pages, len(doc))):
            text = doc[page_num].get_text() or ""
            if REFERENCES_HEADING_RE.search(text):
                text = REFERENCES_HEADING_RE.split(text, maxsplit=1)[0]
            chunks.append(text)
        return "\n".join(chunks)
    finally:
        doc.close()


def _clean_local_title_line(line: str) -> str:
    line = re.sub(r"\s+", " ", line.strip())
    line = re.sub(r"^#{1,6}\s*", "", line)
    return line.strip()


def _is_obvious_non_title_line(line: str) -> bool:
    if not line:
        return True
    if re.search(r"10\.\d{4,}/", line, re.IGNORECASE):
        return True
    if re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", line):
        return True
    if re.match(
        r"^(abstract|keywords?|introduction|references?|bibliography|"
        r"copyright|received|accepted|published|doi\b|http|www\.)",
        line,
        re.IGNORECASE,
    ):
        return True
    if re.search(r"(university|institute|department|laboratory|college|school)", line, re.IGNORECASE):
        return True
    return False


def _extract_pdf_title_candidate(pdf_path: Path) -> str:
    """Extract a conservative first-pages title fallback from PDF text."""
    text = _extract_pdf_first_pages_text(pdf_path, max_pages=3)
    for raw in text.splitlines()[:80]:
        line = _clean_local_title_line(raw)
        if len(line) < 16 or _is_obvious_non_title_line(line):
            continue
        if len(line.split()) > 35:
            continue
        return line
    return ""


# ── Local evidence extraction ──────────────────────────────────────────

def _local_evidence(metadata: dict, md_path: Path | None, pdf_path: Path | None = None, *, prefer_markdown: bool = False) -> tuple[str, int | None, str, str, list[str], str, str, str, list[str]]:
    """Return local metadata evidence plus title/author source hints.

    Pulls trusted existing metadata first, then converted Markdown first 100
    physical lines, then PDF first-pages title fallback. Markdown DOI evidence is
    limited to that same header/front-matter region before any References
    heading; reference-list DOIs are not local evidence for this paper.

    When ``prefer_markdown`` is True (post-conversion re-resolution), Markdown
    front-matter title/author evidence is preferred even when existing metadata
    already carries a DOI/title — the converted Markdown is the freshest source.
    DOI priority is unchanged: an existing valid metadata DOI still wins.
    """
    local_title = ((metadata.get("title") or {}).get("original") or "").strip()
    existing_metadata_doi = metadata_doi(metadata)
    title_source = "metadata" if local_title else "none"
    local_year = metadata.get("year")
    local_first_author_family = _surname(first_author_family(metadata))
    author_source = "metadata" if local_first_author_family and local_first_author_family != "unknownauthor" else "none"
    if local_first_author_family == "unknownauthor":
        local_first_author_family = ""
    abstract = metadata.get("abstract") or ""

    md_dois: list[str] = []
    md_front_lines: list[str] = []
    if md_path and md_path.exists():
        try:
            md_text = md_path.read_text(encoding="utf-8")
        except Exception:
            md_text = ""
        header_text, _refs = _split_header_and_references(md_text)
        md_dois = collect_dois_from_text(header_text)
        front = extract_front_matter_candidates_from_markdown(md_path, max_lines=100)
        md_front_lines = list(front.front_matter_lines or [])
        # Default: only fall back to Markdown front-matter when metadata lacks
        # DOI/title. With prefer_markdown (post-convert), always prefer the
        # freshly-converted Markdown evidence over stale metadata.
        prefer_front_title = prefer_markdown or (not existing_metadata_doi or not local_title)
        prefer_front_author = prefer_markdown or (not existing_metadata_doi or not local_first_author_family)
        if front.title_candidates and prefer_front_title:
            local_title = front.title_candidates[0]
            title_source = "markdown_front_matter"
        if front.author_candidates:
            first_line = front.author_candidates[0]
            if first_line and prefer_front_author:
                local_first_author_family = _surname(first_line[0])
                author_source = "markdown_front_matter"
        ext = extract_metadata_from_markdown(md_path, paper_name="", max_scan_chars=MD_HEADER_SCAN_CHARS)
        if local_year is None and ext.year_candidates:
            local_year = ext.year_candidates[0]
        if not abstract:
            abstract = ext.abstract_candidate or abstract
    if not local_title and pdf_path and pdf_path.exists():
        pdf_title = _extract_pdf_title_candidate(pdf_path)
        if pdf_title:
            local_title = pdf_title
            title_source = "pdf_fallback"
    return local_title, local_year, local_first_author_family, abstract, md_dois, "", title_source, author_source, md_front_lines
