"""Conservative author/name helpers for metadata resolution.

Used for matching and for the conservative (family, given) split in patch
builders. Matching-only transforms never become metadata facts.
"""
from __future__ import annotations

import re
import unicodedata


def ascii_fold(value: str) -> str:
    nfkd = unicodedata.normalize("NFKD", value)
    return nfkd.encode("ascii", "ignore").decode("ascii")


def is_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def surname(name: str) -> str:
    """Ascii-folded, lowercased last token of a name (for matching only)."""
    if not name:
        return ""
    folded = ascii_fold(name).strip()
    if not folded:
        return ""
    token = re.split(r"[\s,]+", folded)
    token = [t for t in token if t]
    if not token:
        return ""
    return re.sub(r"[^a-z0-9]", "", token[-1].lower())


def split_name(name: str) -> tuple[str, str]:
    """Conservative (family, given) split. Returns ("", "") when unreliable.

    Assumes Western "Given Family" order (as returned by OpenAlex/S2 display
    names). Unreliable cases (return ("","") so the caller stores full_name
    only): empty, single token, CJK characters, all-caps institution-like
    strings, or when the last token is a single initial (ambiguous "Family G"
    citation form — we refuse to guess). Never fabricate a wrong family name.
    """
    if not name:
        return "", ""
    name = name.strip()
    if not name or is_cjk(name):
        return "", ""
    parts = re.split(r"\s+", name)
    if len(parts) < 2:
        return "", ""
    if name.isupper() and len(name) <= 6:
        return "", ""
    last = parts[-1]
    # single-letter initial as last token → ambiguous citation form, refuse
    if len(last) == 1:
        return "", ""
    family = last
    given = " ".join(parts[:-1])
    return family, given
