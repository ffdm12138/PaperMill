"""Catalog keyword identity: validation and definition hashing.

Extracted from ``registry`` so ``registry`` and ``registry_schema`` no
longer import each other (former late-import cycle).
"""
from __future__ import annotations

from src.utils.canonical_json import canonical_sha256
import re
import unicodedata

from src.catalog_folders.exceptions import (
    FilesystemNameCollision,
    InvalidChineseKeyword,
)
from src.catalog_folders.models import CLASSIFIER_SKILL_VERSION

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = {"all", "_pending", ".state", ".", ".."}
_RESERVED_CASEFOLD = {"all", "_pending", ".state"}
_HAS_CJK = re.compile(r"[一-鿿㐀-䶿豈-﫿]")
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def validate_catalog_keyword(keyword: str) -> str:
    """Validate a Chinese keyword is safe for use as a category directory name.

    Returns the NFC-normalized keyword.  Raises an appropriate exception if
    the keyword is invalid.  The original input is checked first against
    strict formatting rules before any normalization is applied.
    """
    original = str(keyword)

    # ── strict pre-normalization checks on original input ──────────────
    if original != original.strip():
        raise InvalidChineseKeyword(
            f"category keyword has leading/trailing whitespace: {original!r}"
        )
    if original != original.rstrip("."):
        raise InvalidChineseKeyword(
            f"category keyword has trailing dots: {original!r}"
        )
    if "/" in original or "\\" in original:
        raise FilesystemNameCollision(
            f"category keyword contains path separators: {original!r}"
        )
    if any(ord(c) < 0x20 for c in original):
        raise InvalidChineseKeyword(
            f"category keyword contains control characters: {original!r}"
        )
    if original.upper() in _WIN_RESERVED:
        raise FilesystemNameCollision(
            f"category keyword is a Windows reserved name: {original!r}"
        )
    if unicodedata.normalize("NFC", original) != original:
        raise InvalidChineseKeyword(
            f"category keyword must be NFC-normalized Unicode: {original!r}"
        )
    if original.casefold() in _RESERVED_CASEFOLD:
        raise FilesystemNameCollision(
            f"category keyword collides with reserved name: {original!r}"
        )

    # ── normalize and validate content ─────────────────────────────────
    text = original.strip()
    if not text:
        raise InvalidChineseKeyword("category keyword must not be empty")
    if not _HAS_CJK.search(text):
        raise InvalidChineseKeyword(
            f"category keyword must contain at least one Chinese character: {keyword!r}"
        )
    if _UNSAFE.search(text):
        raise FilesystemNameCollision(
            f"category keyword contains unsafe filesystem characters: {keyword!r}"
        )
    if text.casefold() in _RESERVED:
        raise FilesystemNameCollision(
            f"category keyword is a reserved name: {keyword!r}"
        )
    if len(text) > 64:
        raise InvalidChineseKeyword(
            f"category keyword too long (max 64): {len(text)}"
        )
    return text



def _is_empty(val: object) -> bool:
    """Return True for None, empty string, empty list, or empty tuple."""
    return val is None or val in ("", [], ())



def definition_hash(value: dict) -> str:
    """Hash only Chinese-relevant definition fields plus the classifier
    contract version.

    Chinese and English search queries, provider cursors, and file paths are
    explicitly excluded — changing them must not invalidate decisions.
    """
    keys = ("category_id", "keyword_zh", "guidance_zh", "aliases_zh", "exclusions_zh")
    payload = {key: value.get(key) for key in keys if not _is_empty(value.get(key))}
    payload["classifier_skill_version"] = CLASSIFIER_SKILL_VERSION
    return canonical_sha256(payload)


