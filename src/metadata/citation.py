"""Deterministic BibTeX and human-reference rendering from Metadata only."""
from __future__ import annotations

import re
from typing import Any

from src.metadata.schema import first_author_family
from src.naming import sanitize_paper_name


def _field(metadata: dict, path: tuple[str, ...], default: Any = "") -> Any:
    value: Any = metadata
    for key in path:
        if not isinstance(value, dict):
            return default
        value = value.get(key)
    return default if value is None else value


def bibtex_from_metadata(metadata: dict, *, key: str | None = None) -> str:
    title = _field(metadata, ("title", "original"), "Untitled")
    year = metadata.get("year") or ""
    doi = str(_field(metadata, ("identifiers", "doi"), "") or "").strip()
    journal = _field(metadata, ("container", "journal"), "")
    booktitle = _field(metadata, ("container", "booktitle"), "") or _field(metadata, ("container", "conference"), "")
    publisher = _field(metadata, ("container", "publisher"), "")
    institution = _field(metadata, ("container", "institution"), "") or _field(metadata, ("container", "school"), "")
    volume = _field(metadata, ("publication", "volume"), "")
    number = _field(metadata, ("publication", "number"), "") or _field(metadata, ("publication", "issue"), "")
    pages = _field(metadata, ("publication", "pages"), "")
    article_number = _field(metadata, ("publication", "article_number"), "")
    url = _field(metadata, ("links", "url"), "")
    isbn = _field(metadata, ("identifiers", "isbn"), "")
    author_text = " and ".join(
        author.get("full_name") or " ".join(part for part in (author.get("given", ""), author.get("family", "")) if part)
        if isinstance(author, dict) else str(author)
        for author in metadata.get("authors") or []
    )
    key = key or f"{first_author_family(metadata).lower()}{year or 'nd'}"
    entry = str(metadata.get("entry_type") or "article").casefold()
    bib_type = {
        "article": "article", "inproceedings": "inproceedings", "paper-conference": "inproceedings",
        "incollection": "incollection", "chapter": "incollection", "book": "book",
        "phdthesis": "phdthesis", "mastersthesis": "mastersthesis", "thesis": "phdthesis",
        "techreport": "techreport", "report": "techreport", "preprint": "misc", "misc": "misc",
    }.get(entry, "misc")
    fields = [("title", title), ("author", author_text)]
    if journal:
        fields.append(("journal", journal))
    elif booktitle:
        fields.append(("booktitle", booktitle))
    fields.extend([
        ("year", year), ("volume", volume), ("number", number), ("pages", pages),
        ("article-number", article_number), ("doi", doi), ("url", url),
        ("publisher", publisher), ("institution", institution), ("isbn", isbn),
    ])
    lines = [f"@{bib_type}{{{sanitize_paper_name(str(key))},"]
    lines.extend(f"  {name} = {{{value}}}," for name, value in fields if value)
    lines.append("}")
    return "\n".join(lines)


def _initials(given: str) -> str:
    result: list[str] = []
    for part in re.split(r"[\s\-]+", str(given).strip()):
        clean = re.sub(r"[^A-Za-z]", "", part)
        if clean:
            result.append(f"{clean[0].upper()}.")
    return " ".join(result)


def _apa_author(author: Any) -> str:
    if not isinstance(author, dict):
        text = str(author).strip()
        parts = text.split()
        return f"{parts[-1]}, {_initials(' '.join(parts[:-1]))}" if len(parts) > 1 else text
    family = str(author.get("family") or "").strip()
    given = str(author.get("given") or "").strip()
    if not family:
        parts = str(author.get("full_name") or "").split()
        if parts:
            family, given = parts[-1], " ".join(parts[:-1])
    initials = _initials(given)
    return f"{family}, {initials}".strip().rstrip(",") if initials else family


def format_reference_from_metadata(metadata: dict, style: str = "apa") -> str:
    if style.casefold() != "apa":
        raise ValueError(f"unsupported reference style: {style}")
    authors = [_apa_author(author) for author in metadata.get("authors") or []]
    authors = [author for author in authors if author]
    if len(authors) == 1:
        author_text = authors[0]
    elif len(authors) == 2:
        author_text = f"{authors[0]}, & {authors[1]}"
    else:
        author_text = f"{', '.join(authors[:-1])}, & {authors[-1]}" if authors else ""
    year = metadata.get("year") or "n.d."
    title = _field(metadata, ("title", "original"), "")
    container = _field(metadata, ("container", "journal"), "") or _field(metadata, ("container", "booktitle"), "") or _field(metadata, ("container", "conference"), "") or _field(metadata, ("container", "institution"), "")
    volume = _field(metadata, ("publication", "volume"), "")
    issue = _field(metadata, ("publication", "issue"), "") or _field(metadata, ("publication", "number"), "")
    pages = _field(metadata, ("publication", "pages"), "") or _field(metadata, ("publication", "article_number"), "")
    doi = str(_field(metadata, ("identifiers", "doi"), "") or "").strip()
    url = str(_field(metadata, ("links", "url"), "") or "").strip()
    parts = [f"{author_text} ({year})." if author_text else f"({year}).", f"{title}."]
    if container:
        publication = container + (f", {volume}" if volume else "") + (f"({issue})" if issue else "") + (f", {pages}" if pages else "")
        parts.append(publication + ".")
    if doi:
        parts.append(f"https://doi.org/{doi}")
    elif url:
        parts.append(url)
    return " ".join(part for part in parts if part)
