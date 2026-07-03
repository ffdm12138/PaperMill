"""Checkpoint manager for metadata resolve — supports resume after 429/timeout.

The checkpoint is a JSON file that records per-paper_number resolve status so a
long 187-paper run can be resumed after a rate-limit/timeout interruption
without re-processing papers that are already matched/citation-ready.

Schema:
{
  "schema_version": "1.0",
  "started_at": "...",
  "updated_at": "...",
  "items": {
    "0000000000000001": {
      "status": "matched | unmatched | skipped | failed | rate_limited",
      "attempts": 1,
      "last_provider": "crossref",
      "last_error": "",
      "updated_at": "..."
    }
  }
}
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from src.utils.atomic_io import atomic_write_json


CHECKPOINT_VERSION = "1.0"

# Items in these statuses are skipped on --resume (already done).
DONE_STATUSES = {"matched", "skipped"}
# Items in these statuses are retried on --resume.
RETRY_STATUSES = {"failed", "rate_limited", "unmatched"}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_checkpoint(path: str | Path) -> dict[str, Any]:
    """Load a checkpoint file; return an empty skeleton if absent or invalid."""
    p = Path(path)
    if not p.exists():
        return {"schema_version": CHECKPOINT_VERSION, "started_at": _now_iso(), "updated_at": _now_iso(), "items": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"schema_version": CHECKPOINT_VERSION, "started_at": _now_iso(), "updated_at": _now_iso(), "items": {}}
    if not isinstance(data, dict):
        return {"schema_version": CHECKPOINT_VERSION, "started_at": _now_iso(), "updated_at": _now_iso(), "items": {}}
    data.setdefault("schema_version", CHECKPOINT_VERSION)
    data.setdefault("started_at", _now_iso())
    data.setdefault("updated_at", _now_iso())
    data.setdefault("items", {})
    return data


def save_checkpoint(path: str | Path, data: dict[str, Any]) -> None:
    """Atomically write a checkpoint file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data["schema_version"] = CHECKPOINT_VERSION
    data["updated_at"] = _now_iso()
    atomic_write_json(p, data, indent=2)


def record_item(
    data: dict[str, Any],
    paper_number: str,
    *,
    status: str,
    attempts: int | None = None,
    last_provider: str = "",
    last_error: str = "",
) -> dict[str, Any]:
    """Record/update a single paper_number in the checkpoint data (in place).

    Returns the item dict that was written.
    """
    items = data.setdefault("items", {})
    existing = items.get(paper_number, {}) if isinstance(items.get(paper_number), dict) else {}
    if attempts is None:
        attempts = int(existing.get("attempts", 0)) + 1
    item = {
        "status": status,
        "attempts": attempts,
        "last_provider": last_provider or existing.get("last_provider", ""),
        "last_error": last_error or existing.get("last_error", ""),
        "updated_at": _now_iso(),
    }
    items[paper_number] = item
    data["updated_at"] = _now_iso()
    return item


def is_done(data: dict[str, Any], paper_number: str) -> bool:
    """True when the paper_number is in a DONE status (skip on resume)."""
    item = data.get("items", {}).get(paper_number)
    if not isinstance(item, dict):
        return False
    return item.get("status") in DONE_STATUSES


def item_attempts(data: dict[str, Any], paper_number: str) -> int:
    item = data.get("items", {}).get(paper_number)
    if not isinstance(item, dict):
        return 0
    return int(item.get("attempts", 0))
