"""Shared canonical normalization for independent identity evidence."""
from __future__ import annotations
import html, re, unicodedata
from typing import Any

def canonical_title(value: str) -> str:
    value = html.unescape(unicodedata.normalize("NFC", value or "")).casefold()
    value = re.sub(r"\\(?:text|mathrm|mathbf|operatorname)\s*\{([^{}]*)\}", r"\1", value)
    value = re.sub(r"[—–−]", "-", value)
    value = value.replace("：", ":").replace("（", "(").replace("）", ")")
    return re.sub(r"[^\w\u3400-\u9fff]+", " ", value).strip()


def _is_effectively_empty(value: Any) -> bool:
    if value in (None, "", [], {}):
        return True
    if isinstance(value, list):
        return all(isinstance(item, dict) and all(child in (None, "", [], {}) for child in item.values()) for item in value)
    return False


def merge_missing_metadata(base: dict, patch: dict) -> tuple[dict, list[str]]:
    """Fill only empty bibliographic fields and report preserved conflicts."""
    warnings: list[str] = []

    def merge(destination: Any, source: Any, path: str) -> Any:
        if isinstance(destination, dict) and isinstance(source, dict):
            result = dict(destination)
            for key, value in source.items():
                child = f"{path}.{key}" if path else key
                result[key] = value if key not in result or _is_effectively_empty(result[key]) else merge(result[key], value, child)
            return result
        if isinstance(destination, list) and isinstance(source, list) and len(destination) == len(source) and all(isinstance(item, dict) for item in (*destination, *source)):
            return [merge(left, right, f"{path}[{index}]") for index, (left, right) in enumerate(zip(destination, source))]
        if _is_effectively_empty(destination):
            return source
        if not _is_effectively_empty(source) and destination != source:
            warnings.append(f"preserved non-empty metadata field: {path}")
        return destination

    return merge(base, patch, ""), warnings
