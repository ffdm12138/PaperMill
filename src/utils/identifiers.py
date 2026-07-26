"""Shared identifier rules: DOIs, normalized titles, 16-digit paper numbers.

Single source for
- DOI normalization (``normalize_doi``) and the DOI candidate/trailing
  regex family (``extract_doi_from_text`` / ``collect_dois_from_text``),
- title normalization for identity comparison (``normalize_title``),
- the permanent 16-digit ``paper_number`` machine-identity rules.

Domain packages import from here; private copies of these regexes are
forbidden (they historically drifted between three implementations).
"""
from __future__ import annotations

import re

# ── DOI ────────────────────────────────────────────────────────────────

DOI_PREFIX = "https://doi.org/"

# Matches: 10.xxxx/xxxxx, https://doi.org/10.xxxx/xxxxx, doi:10.xxxx/xxxxx,
# DOI 10.xxxx/xxxxx — group 1 is the bare DOI.
DOI_CANDIDATE_RE = re.compile(
    r"""(?ix)
    (?:doi\s*[:=\s]+)?               # optional "doi:" or "DOI " prefix
    (?:https?://(?:dx\.)?doi\.org/)? # optional https://doi.org/
    (10\.\d{4,}/[^\s<>"')\]};,]+)    # the DOI itself
    """,
)

# Trailing punctuation to strip from DOI matches.
DOI_TRAILING_RE = re.compile(r"""[.,;)\]};:'"]+$""")


def normalize_doi(doi: str | None) -> str:
    if not doi:
        return ""
    value = doi.strip()
    if value.lower().startswith(DOI_PREFIX):
        value = value[len(DOI_PREFIX):]
    return value.strip().lower()


def extract_doi_from_text(text: str) -> str | None:
    """Extract and normalize the first DOI found in arbitrary text.

    Returns the normalized DOI (lowercase, no prefix) or ``None``.
    """
    if not text:
        return None
    match = DOI_CANDIDATE_RE.search(text)
    if not match:
        return None
    raw = DOI_TRAILING_RE.sub("", match.group(1))
    normalized = normalize_doi(raw)
    if not normalized or "/" not in normalized:
        return None
    return normalized


def collect_dois_from_text(text: str) -> list[str]:
    """Collect ALL distinct normalized DOIs from text, in first-seen order."""
    seen: list[str] = []
    for match in DOI_CANDIDATE_RE.finditer(text or ""):
        raw = DOI_TRAILING_RE.sub("", match.group(1))
        norm = normalize_doi(raw)
        if norm and "/" in norm and norm not in seen:
            seen.append(norm)
    return seen


# ── Title ──────────────────────────────────────────────────────────────

def normalize_title(title: str | None) -> str:
    if not title:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", title.lower())).strip()


# ── Paper number ───────────────────────────────────────────────────────

PAPER_NUMBER_RE = re.compile(r"^\d{16}$")


def is_paper_number(value: object) -> bool:
    return bool(PAPER_NUMBER_RE.match(str(value or "")))


def validate_paper_raw_id(value: object) -> str:
    text = str(value or "")
    if is_paper_number(text):
        return text
    raise ValueError("paper_number must be 16 digits")
