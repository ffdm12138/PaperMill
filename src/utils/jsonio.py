"""Canonical JSON read primitives.

Two contracts cover every reader in the codebase:

- :func:`read_json` — tolerant: a missing file or invalid JSON returns
  ``default``. For sidecars where absence/corruption means "no data yet".
- :func:`read_json_strict` — strict: missing file or invalid JSON raises.
  For artifacts whose absence is a hard error (frozen metadata, markers).

Callers with a narrower contract (e.g. "must be a JSON object") keep a thin
local adapter that delegates to one of these primitives; they must not
reimplement file reading or exception policy.

The ledger keeps its own ``orjson`` fast path (`paper_number_ledger._read_json`)
— a deliberate performance exemption, not a duplicate.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_json(path: str | Path, default: Any = None) -> Any:
    """Tolerant read: missing file or invalid JSON returns ``default``."""
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def read_json_strict(path: str | Path) -> Any:
    """Strict read: raises on a missing file or invalid JSON."""
    return json.loads(Path(path).read_text(encoding="utf-8"))
