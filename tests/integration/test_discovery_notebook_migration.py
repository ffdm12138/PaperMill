from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.migrate_discovery_notebooks_v2 import migrate_file
from src.discovery.keyword_notebook import INITIAL_CURSOR, SCHEMA_VERSION


pytestmark = pytest.mark.integration


def test_v1_notebook_migration_is_idempotent_and_resets_cursor(tmp_path: Path):
    path = tmp_path / "kw__12345678.json"
    payload = {
        "schema_version": "1.0",
        "keyword_id": "kid",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "expansions": {
            "exp": {
                "providers": {
                    "openalex": {"backfill": {"cursor": "UNSAFE", "exhausted": True, "pages_succeeded": 9, "items_returned_total": 99}},
                    "crossref": {"backfill": {"cursor": "OLD"}},
                }
            }
        },
        "lifetime_statistics": {"refresh_runs": 1, "backfill_runs": 2},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = migrate_file(path, apply=True)
    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "migrated"
    assert migrated["schema_version"] == SCHEMA_VERSION
    assert migrated["expansions"]["exp"]["providers"]["openalex"]["backfill"]["cursor"] == INITIAL_CURSOR
    archives = list((tmp_path / "archive_v1").glob("*.json"))
    assert len(archives) == 1

    result2 = migrate_file(path, apply=True)
    assert result2["status"] == "already_v2"
    assert len(list((tmp_path / "archive_v1").glob("*.json"))) == 1
