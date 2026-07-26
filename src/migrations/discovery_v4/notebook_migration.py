"""Notebook config migration from v3 to v4.

Parses each legacy schema-3.0 notebook through the strict
``LegacyNotebookV3`` contract (the production ``validate_notebook`` rejects
legacy schema versions by design and is never called on v3 input), then
converts it with ``convert_notebook_v3_to_v4``, which validates the
converted v4 product with the production ``validate_notebook`` and
``validate_discovery_readiness`` before anything is written.

Migration rules (enforced by ``convert_notebook_v3_to_v4``):
- KEEP: keyword_zh, enabled, search_queries (query text + language + source),
  relevance_profile, classification
- RESET: cursor=*, exhausted=false, generation=1, all counter fields=0,
  generation_history=[], last_* fields=null
- VERIFY: keyword_id matches canonical computation
- ADD: migration_history entry recording this migration
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from src.migrations.discovery_v4.legacy_contracts.notebook_v3 import (
    LegacyNotebookV3,
    convert_notebook_v3_to_v4,
)


def migrate_all_notebooks(
    notebook_dir: Path,
    output_dir: Path,
) -> list[dict[str, Any]]:
    """Migrate all v3 notebooks to v4 and write to output directory.

    Args:
        notebook_dir: Source directory with v3 notebook JSON files.
        output_dir: Destination for v4 notebook JSON files.

    Returns:
        List of migration results per notebook.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    for path in sorted(Path(notebook_dir).glob("*.json")):
        if path.name == "archive_manifest.json":
            # Legacy archive snapshots co-locate their archive manifest with
            # the notebooks; it is not a notebook.
            continue
        entry: dict[str, Any] = {
            "source": str(path),
            "keyword_zh": "unknown",
            "keyword_id": "unknown",
            "success": False,
            "error": None,
        }

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            legacy = LegacyNotebookV3.from_dict_strict(raw)
            entry["keyword_zh"] = legacy.keyword_zh
            entry["keyword_id"] = legacy.keyword_id

            # Conversion validates the v4 product (schema + discovery
            # readiness) and fails closed before anything is written.
            v4 = convert_notebook_v3_to_v4(legacy)

            # Write v4 notebook — use same filename convention
            keyword_zh = legacy.keyword_zh
            kid = legacy.keyword_id
            fp8 = kid[:8]
            out_path = output_dir / f"{keyword_zh}__{fp8}.json"

            payload = json.dumps(v4, ensure_ascii=False, indent=2)
            raw_bytes = payload.encode("utf-8")
            tmp = out_path.with_suffix(out_path.suffix + ".tmp")
            try:
                with tmp.open("wb") as fh:
                    fh.write(raw_bytes)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(str(tmp), str(out_path))
            except Exception:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise

            # Count active queries for report (from the validated v4 product)
            active_queries = [
                q for q in v4["search_queries"].values() if q["active"]
            ]
            entry.update({
                "success": True,
                "output": str(out_path),
                "active_queries": len(active_queries),
                "lane_count": len(active_queries) * 2 * 2,  # queries × providers × modes
            })
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"

        results.append(entry)

    return results
