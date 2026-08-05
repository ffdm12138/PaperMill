"""Shared identifier rules: DOIs, normalized titles, 16-digit paper numbers.

Single source for
- DOI normalization (``normalize_doi``) and the DOI candidate/trailing
  regex family (``extract_doi_from_text`` / ``collect_dois_from_text``),
- noisy-candidate DOI cleaning for PDF-derived text
  (``clean_extracted_doi_candidate`` / ``extract_doi_candidates`` /
  ``join_line_broken_doi_lines`` / ``is_valid_doi``) — this layer never
  changes the meaning of ``normalize_doi``; it only turns noisy raw
  candidates (trailing punctuation, line breaks, unbalanced brackets,
  placeholders, truncated fragments) into trusted normalized DOIs,
- title normalization for identity comparison (``normalize_title``),
- the permanent 16-digit ``paper_number`` machine-identity rules.

Domain packages import from here; private copies of these regexes are
forbidden (they historically drifted between three implementations).
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote

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

# Minimal plausible DOI length ("10.1234/x" is too short to be real).
DOI_MIN_LENGTH = 10

# Syntactic gate for cleaned candidates: 10.<4-9 digit prefix>/<suffix>.
DOI_VALID_RE = re.compile(r"^10\.\d{4,9}/[A-Za-z0-9._\-():;/]+$")

# Placeholder runs like "10.1073/pnas.xxxxxxxxxx" from template PDFs.
DOI_PLACEHOLDER_RUN_RE = re.compile(r"x{5,}", re.IGNORECASE)

# Prefixes that may decorate a raw candidate before the bare DOI.
_DOI_DECORATION_PREFIXES = (
    "https://doi.org/",
    "http://dx.doi.org/",
    "https://dx.doi.org/",
    "doi:",
)


def normalize_doi(doi: str | None) -> str:
    if not doi:
        return ""
    value = doi.strip()
    if value.lower().startswith(DOI_PREFIX):
        value = value[len(DOI_PREFIX):]
    return value.strip().lower()


def is_valid_doi(doi: str | None) -> bool:
    """Syntactic gate for a cleaned DOI candidate.

    Rejects fragments and placeholders that a wide regex would accept
    (``10.1103/P``, ``10.1073/pnas.``, ``10.1073/pnas.xxxxxxxxxx``).
    Only used for extracted candidates; never changes global DOI semantics.
    """
    if not doi:
        return False
    value = doi.strip().lower()
    if not DOI_VALID_RE.match(value):
        return False
    if len(value) < DOI_MIN_LENGTH:
        return False
    if value.endswith("."):
        return False
    if DOI_PLACEHOLDER_RUN_RE.search(value):
        return False
    suffix = value.split("/", 1)[1]
    if not re.search(r"[A-Za-z0-9]", suffix):
        return False
    # A short pure-alphabetic suffix is a truncated fragment, not an
    # article identifier ("pnas" from "10.1073/pnas.…", "nature" from
    # "10.1038/nature…").  A real suffix carries a digit or is long
    # enough to be a genuine journal-code-plus-id form.
    if not re.search(r"\d", suffix) and len(suffix) < 10:
        return False
    return True


def _balance_trim(value: str) -> str:
    """Trim trailing unmatched ``)`` / ``]`` / ``(`` / ``[`` characters."""
    while value:
        opens = value.count("(") + value.count("[")
        closes = value.count(")") + value.count("]")
        if closes > opens and value[-1] in ")]":
            value = value[:-1]
            continue
        if opens > closes and value[-1] in "([":
            value = value[:-1]
            continue
        break
    return value


def clean_extracted_doi_candidate(raw: str | None) -> str | None:
    """Turn a noisy raw DOI candidate into a trusted normalized DOI.

    Handles: decoration prefixes, URL-decoding, Unicode NFC, unbalanced
    brackets, trailing punctuation, line-break trailing hyphens, and
    fragment/placeholder rejection.  Returns ``None`` when the candidate
    does not survive ``is_valid_doi``.  ``normalize_doi`` itself is
    untouched — this is the only entry point that feeds it noisy input.
    """
    if not raw:
        return None
    value = raw.strip()
    lowered = value.lower()
    for prefix in _DOI_DECORATION_PREFIXES:
        if lowered.startswith(prefix):
            value = value[len(prefix):]
            break
    else:
        match = re.match(r"(?i)^doi[\s:=]+", value)
        if match:
            value = value[match.end():]
    value = unquote(value)
    value = unicodedata.normalize("NFC", value)
    # Unbalanced close brackets first, then trailing punctuation, then
    # re-check balance (e.g. "…abc.)" -> "…abc").
    value = _balance_trim(value)
    value = DOI_TRAILING_RE.sub("", value)
    value = _balance_trim(value)
    # A trailing hyphen is a line-break artifact, never a valid suffix end.
    value = value.rstrip("-")
    value = value.strip()
    if not is_valid_doi(value):
        return None
    return normalize_doi(value)


def extract_doi_candidates(text: str | None) -> list[str]:
    """Collect ALL distinct cleaned normalized DOIs from noisy text.

    Unlike ``collect_dois_from_text`` this applies the full noisy-candidate
    cleaning pipeline (fragments and placeholders rejected).
    """
    seen: list[str] = []
    for match in DOI_CANDIDATE_RE.finditer(text or ""):
        cleaned = clean_extracted_doi_candidate(match.group(1))
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen


def join_line_broken_doi_lines(lines: list[str] | tuple[str, ...]) -> list[str]:
    """Reconcile DOIs split across PDF/markdown text lines.

    When a line ends with an unfinished DOI suffix (no terminating
    punctuation) and the next line continues it, the joined candidate is
    cleaned and appended.  Existing whole-line candidates are kept.
    """
    results: list[str] = []
    for index, line in enumerate(lines):
        results.extend(extract_doi_candidates(line))
        if index + 1 < len(lines):
            tail = line.strip()
            # Only join when the tail looks like a genuinely unfinished
            # suffix: at least 8 chars (real line-break tails are long)
            # and the next line does not itself start a new DOI ("10.").
            fragment = re.search(r"10\.\d{4,9}/[^\s<>\"')\]};,]{8,}$", tail)
            if fragment and len(fragment.group(0)) >= 8:
                head = lines[index + 1].strip()
                if re.match(r"(?i)10\.", head):
                    continue
                joined = fragment.group(0) + head[:40]
                for candidate in extract_doi_candidates(joined):
                    if candidate not in results:
                        results.append(candidate)
    return results


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
