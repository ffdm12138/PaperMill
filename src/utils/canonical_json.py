"""Canonical JSON encoding + sha256 (single source).

Family-A encoding only: ``ensure_ascii=False``, ``sort_keys=True``, compact
separators ``(",", ":")``.  Persisted hashes elsewhere that use a DIFFERENT
byte encoding (default separators, ``indent=2``, ``[:16]`` truncation,
string-join payloads) are deliberately NOT unified — changing their bytes
would corrupt stored fingerprints; each such site carries a local comment.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json_bytes(value: Any) -> bytes:
    """Compact, sorted, UTF-8 canonical JSON bytes for hashing."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(value: Any, *, length: int | None = None) -> str:
    """sha256 hex digest of :func:`canonical_json_bytes`.

    ``length`` truncates the hex digest (some persisted identities use a
    16-hex prefix); ``None`` returns the full 64-hex digest.
    """
    digest = hashlib.sha256(canonical_json_bytes(value)).hexdigest()
    return digest if length is None else digest[:length]
