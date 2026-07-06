"""Canonical paper_id generation helpers (v2).

v2 中正式 paper_id 由 metadata + catalog 生成（catalog v3.1 不再承载书目字段，中文标题在 catalog.content_identity.content_title_zh），
不依赖 filename fallback，也不存在 sidecar 优先级链：

  - 正式入库：``paper_id_from_metadata_catalog(metadata, catalog)``
    = metadata.year + metadata.first_author.family + catalog.content_identity.content_title_zh
    （见 src/services/v2_library.py）。
  - metadata 富化阶段可先用本模块的 ``generate_paper_id`` 生成一个
    proposed_paper_id（year + first_author + chinese_title/title_slug），
    仅供预览，最终以 curation 后的正式 paper_id 为准。

filename fallback 不允许用于正式入库。
"""
from __future__ import annotations

import re

from src.naming import sanitize_paper_id, validate_paper_id


def _slug(text: str, max_len: int = 50) -> str:
    text = text or ""
    slug = re.sub(r"[^一-鿿A-Za-z0-9]+", "_", text).strip("_").lower()
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("_")
    return slug or "untitled"


def _first_author_slug(authors: list | str | None) -> str:
    if not authors:
        return ""
    first = authors[0] if isinstance(authors, list) else str(authors).split(",", 1)[0]
    if isinstance(first, dict):
        first = first.get("last") or first.get("family") or first.get("name") or ""
    first = str(first).strip()
    if not first:
        return ""
    # If "Family, Given" format, take family name
    if "," in first:
        first = first.split(",", 1)[0].strip()
    return _slug(first, max_len=24)


def generate_paper_id(
    *,
    year: int | None,
    title: str,
    authors: list | str | None = None,
    chinese_title: str = "",
) -> str:
    """Generate a filesystem-safe paper_id.

    Priority:
      1. year + first_author + (chinese_title or title_slug)
      2. year + title_slug
      3. first_author + title_slug
      4. title_slug only (fallback)

    If author metadata is missing, preserve the historic fallback
    ``{year}_{title_slug}`` to avoid surprising existing imports.
    """
    title_slug = _slug(chinese_title or title, max_len=50)
    author_slug = _first_author_slug(authors)
    if year and author_slug:
        candidate = f"{year}_{author_slug}_{title_slug}"
    elif year:
        candidate = f"{year}_{title_slug}"
    elif author_slug:
        candidate = f"{author_slug}_{title_slug}"
    else:
        candidate = title_slug
    candidate = sanitize_paper_id(candidate)
    validate_paper_id(candidate)
    return candidate
