from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from src.catalog_folders.models import Category
from src.utils.atomic_io import atomic_write_json


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"all", "_pending", ".state", ".", ".."}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_keyword(value: str) -> str:
    text = unicodedata.normalize("NFC", str(value)).strip().rstrip(". ")
    text = re.sub(r"\s+", "_", _UNSAFE.sub("_", text))
    text = re.sub(r"_+", "_", text).strip("_")
    if not text or text.casefold() in _RESERVED:
        raise ValueError(f"unsafe category keyword: {value!r}")
    return text[:64].rstrip(". ")


def definition_hash(value: dict) -> str:
    keys = ("category_id", "keyword_zh", "normalized_keyword_zh", "guidance_zh", "aliases_zh", "exclusions_zh")
    payload = {key: value.get(key) for key in keys if value.get(key) not in (None, [], "")}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def category_from_notebook(path: Path) -> Category:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    category_id = str(data.get("keyword_id") or "")
    keyword = str(data.get("keyword") or "").strip()
    normalized = str(data.get("normalized_keyword") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{16}", category_id):
        raise ValueError(f"invalid keyword_id in {path}")
    if not keyword or not normalized:
        raise ValueError(f"notebook lacks Chinese keyword fields: {path}")
    base = {
        "category_id": category_id, "keyword_zh": keyword,
        "normalized_keyword_zh": normalized,
    }
    return Category(
        **base, directory_name=f"{safe_keyword(keyword)}__{category_id[:8]}",
        source_notebook=Path(path).name, definition_sha256=definition_hash(base),
    )


def load_registry(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema_version") != "1.0" or not isinstance(data.get("categories"), list):
        raise ValueError("invalid category registry")
    return data


def sync_registry(*, notebook_dir: Path, registry_path: Path, apply: bool) -> dict:
    existing = load_registry(registry_path) if registry_path.is_file() else {"categories": []}
    by_id = {str(row.get("category_id")): dict(row) for row in existing["categories"] if isinstance(row, dict)}
    added: list[str] = []
    changed: list[str] = []
    for notebook in sorted(Path(notebook_dir).glob("*.json")):
        category = category_from_notebook(notebook).to_dict()
        old = by_id.get(category["category_id"])
        if old is None:
            by_id[category["category_id"]] = category
            added.append(category["category_id"])
        else:
            enriched = {**category, **{key: old[key] for key in ("guidance_zh", "aliases_zh", "exclusions_zh", "retired_at", "classification_enabled") if key in old}}
            enriched["definition_sha256"] = definition_hash(enriched)
            if enriched != old:
                changed.append(category["category_id"])
            by_id[category["category_id"]] = enriched
    result = {"schema_version": "1.0", "updated_at": now_iso(), "categories": [by_id[key] for key in sorted(by_id)]}
    if apply:
        atomic_write_json(registry_path, result, indent=2)
    return {"registry": result, "added": added, "changed": changed}
