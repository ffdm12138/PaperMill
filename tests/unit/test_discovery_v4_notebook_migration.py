"""Real-path tests for the legacy v3 -> v4 notebook migration.

These tests never mock ``migrate_all_notebooks``: they run the strict
``LegacyNotebookV3`` contract and the real converter against sanitized
schema-3.0 fixtures that mirror the production notebook structure.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.discovery.contracts.notebook import (
    keyword_id as compute_keyword_id,
    validate_discovery_readiness,
    validate_notebook,
)
from src.migrations.discovery_v4.legacy_contracts import (
    LegacyNotebookContractError,
    LegacyNotebookV3,
    convert_notebook_v3_to_v4,
)
from src.migrations.discovery_v4.notebook_migration import migrate_all_notebooks

FIXTURE_DIR = (
    Path(__file__).resolve().parents[1]
    / "fixtures" / "discovery_v4_legacy" / "notebooks"
)
INVALID_FIXTURE_DIR = FIXTURE_DIR.parent / "notebooks_invalid"

EXPECTED_KEYWORDS = {
    "大气边界层", "风沙动力学", "风沙物理学", "风雪动力学", "风雪物理学",
}
_BACKFILL_COUNTER_KEYS = (
    "pages_succeeded", "pages_committed", "items_returned_total",
    "last_page_count", "cursor_conflicts", "consecutive_failures",
)
_BACKFILL_NULL_KEYS = (
    "last_success_at", "last_error", "last_failure_at", "last_error_type",
    "next_retry_at", "terminal_failure_at",
)


def _load_fixture(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _fixture_paths() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("*.json"))


def test_fixture_directory_covers_five_production_keywords():
    paths = _fixture_paths()
    assert len(paths) == 5
    notebooks = [_load_fixture(p) for p in paths]
    assert {nb["keyword_zh"] for nb in notebooks} == EXPECTED_KEYWORDS
    for path, nb in zip(paths, notebooks):
        assert nb["schema_version"] == "3.0"
        assert nb["keyword_id"] == compute_keyword_id(nb["keyword_zh"])
        assert path.name == f"{nb['keyword_zh']}__{nb['keyword_id'][:8]}.json"
        languages = {q["language"] for q in nb["search_queries"].values()}
        assert "zh" in languages and "en" in languages
        assert all(q["active"] for q in nb["search_queries"].values())


class TestLegacyNotebookV3Contract:
    @pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.name)
    def test_real_structure_fixtures_parse(self, path: Path):
        raw = _load_fixture(path)
        legacy = LegacyNotebookV3.from_dict_strict(raw)
        assert legacy.schema_version == "3.0"
        assert legacy.keyword_zh == raw["keyword_zh"]
        assert legacy.keyword_id == compute_keyword_id(raw["keyword_zh"])
        assert legacy.enabled is True
        assert set(legacy.search_queries) == set(raw["search_queries"])
        assert legacy.relevance_profile is not None
        # Round-trip preserves the full field set.
        assert legacy.to_dict() == raw

    def test_rejects_non_dict(self):
        with pytest.raises(LegacyNotebookContractError, match="expected dict"):
            LegacyNotebookV3.from_dict_strict(["not", "a", "dict"])

    @pytest.mark.parametrize(
        "path", sorted(INVALID_FIXTURE_DIR.glob("*.json")), ids=lambda p: p.name
    )
    def test_invalid_fixtures_fail_closed(self, path: Path):
        with pytest.raises(LegacyNotebookContractError):
            LegacyNotebookV3.from_dict_strict(_load_fixture(path))

    def test_invalid_fixtures_fail_for_their_specific_reason(self):
        cases = {
            "missing_required_field.json": "missing keys",
            "unknown_top_level_field.json": "unknown keys",
            "keyword_id_mismatch.json": "does not match keyword_zh",
            "unsupported_schema_2_0.json": "schema_version must be '3.0'",
        }
        for name, needle in cases.items():
            raw = _load_fixture(INVALID_FIXTURE_DIR / name)
            with pytest.raises(LegacyNotebookContractError, match=needle):
                LegacyNotebookV3.from_dict_strict(raw)

    def test_rejects_v4_input(self):
        """A v4 notebook is not legacy input; the legacy gate refuses it."""
        raw = _load_fixture(_fixture_paths()[0])
        raw["schema_version"] = "4.0"
        with pytest.raises(LegacyNotebookContractError, match="schema_version"):
            LegacyNotebookV3.from_dict_strict(raw)

    def test_rejects_unknown_query_field(self):
        raw = _load_fixture(_fixture_paths()[0])
        entry = next(iter(raw["search_queries"].values()))
        entry["unexpected"] = 1
        with pytest.raises(LegacyNotebookContractError, match="unknown keys"):
            LegacyNotebookV3.from_dict_strict(raw)

    def test_rejects_unknown_backfill_field(self):
        raw = _load_fixture(_fixture_paths()[0])
        entry = next(iter(raw["search_queries"].values()))
        entry["providers"]["openalex"]["backfill"]["mystery"] = 1
        with pytest.raises(LegacyNotebookContractError, match="unknown keys"):
            LegacyNotebookV3.from_dict_strict(raw)

    def test_rejects_query_id_map_key_mismatch(self):
        raw = _load_fixture(_fixture_paths()[0])
        key, entry = next(iter(raw["search_queries"].items()))
        raw["search_queries"]["0" * 16] = raw["search_queries"].pop(key)
        with pytest.raises(LegacyNotebookContractError, match="canonical identity"):
            LegacyNotebookV3.from_dict_strict(raw)

    def test_rejects_declared_language_mismatch(self):
        raw = _load_fixture(_fixture_paths()[0])
        entry = next(iter(raw["search_queries"].values()))
        entry["language"] = "en" if entry["language"] == "zh" else "zh"
        with pytest.raises(LegacyNotebookContractError, match="language"):
            LegacyNotebookV3.from_dict_strict(raw)


class TestConvertNotebookV3ToV4:
    @pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.name)
    def test_conversion_preserves_config_and_resets_progress(self, path: Path):
        raw = _load_fixture(path)
        legacy = LegacyNotebookV3.from_dict_strict(raw)
        v4 = convert_notebook_v3_to_v4(legacy)

        # Schema and identity.
        assert v4["schema_version"] == "4.0"
        assert v4["keyword_zh"] == raw["keyword_zh"]
        assert v4["keyword_id"] == raw["keyword_id"]
        assert v4["normalized_keyword_zh"] == raw["normalized_keyword_zh"]
        assert v4["enabled"] == raw["enabled"]
        assert v4["created_at"] == raw["created_at"]

        # Query config preserved exactly (identity, text, language, flags).
        assert set(v4["search_queries"]) == set(raw["search_queries"])
        for qid, src_entry in raw["search_queries"].items():
            dst = v4["search_queries"][qid]
            assert dst["query_id"] == src_entry["query_id"]
            assert dst["query"] == src_entry["query"]
            assert dst["normalized_query"] == src_entry["normalized_query"]
            assert dst["language"] == src_entry["language"]
            assert dst["active"] == src_entry["active"]
            assert dst["source"] == src_entry["source"]
            # Provider progress fully reset on every lane.
            for provider in ("openalex", "crossref"):
                lanes = dst["providers"][provider]
                refresh = lanes["refresh"]
                assert refresh == {
                    "last_started_at": None,
                    "last_success_at": None,
                    "last_status": None,
                    "pages_scanned_last_run": 0,
                    "items_returned_last_run": 0,
                    "last_error": None,
                }
                backfill = lanes["backfill"]
                assert backfill["cursor"] == "*"
                assert backfill["exhausted"] is False
                assert backfill["generation"] == 1
                assert backfill["request_signature"] == ""
                assert backfill["generation_history"] == []
                assert backfill["last_committed_page_id"] == ""
                assert backfill["terminal_failure"] is False
                for key in _BACKFILL_COUNTER_KEYS:
                    assert backfill[key] == 0, key
                for key in _BACKFILL_NULL_KEYS:
                    assert backfill[key] is None, key

        # Relevance profile and classification are config: preserved verbatim.
        assert v4["relevance_profile"] == raw["relevance_profile"]
        assert v4["relevance_profile"]["profile_hash"]
        assert v4["classification"] == raw["classification"]
        assert v4["definition_history"] == raw["definition_history"]
        assert v4["relevance_generation"] == 1

        # Notebook-level progress reset to pristine defaults.
        assert all(v == 0 for v in v4["lifetime_statistics"].values())
        assert v4["pending"] == {
            "pages": 0, "candidates": 0, "last_drained_at": None,
        }
        assert v4["backpressure"] == {
            "active": False, "entered_at": None, "last_pending_count": 0,
            "max_threshold": 1000, "resume_threshold": 700,
        }
        assert v4["reset_history"] == []

        # Migration history appends exactly one entry to the legacy history.
        assert v4["migration_history"][:-1] == raw["migration_history"]
        appended = v4["migration_history"][-1]
        assert appended["from_schema"] == "3.0"
        assert appended["to_schema"] == "4.0"
        assert appended["reason"] == "discovery_v4_one_time_migration"
        assert appended["migrated_at"]

        # The product itself passes the production gates.
        validate_notebook(v4)
        readiness = validate_discovery_readiness(v4)
        assert readiness.ready, readiness.errors

    def test_rejects_non_legacy_input(self):
        with pytest.raises(LegacyNotebookContractError, match="LegacyNotebookV3"):
            convert_notebook_v3_to_v4({"schema_version": "3.0"})

    def test_fails_closed_when_product_not_discovery_ready(self):
        raw = _load_fixture(_fixture_paths()[0])
        # Remove every English query: the legacy input is still structurally
        # valid, but the converted product is not discovery-ready.
        raw["search_queries"] = {
            qid: entry
            for qid, entry in raw["search_queries"].items()
            if entry["language"] != "en"
        }
        legacy = LegacyNotebookV3.from_dict_strict(raw)
        with pytest.raises(
            LegacyNotebookContractError, match="not discovery-ready"
        ):
            convert_notebook_v3_to_v4(legacy)


class TestMigrateAllNotebooksRealPath:
    """End-to-end: the real migrate_all_notebooks, no mocking."""

    def test_all_five_real_structure_fixtures_migrate(self, tmp_path):
        out_dir = tmp_path / "migrated"
        results = migrate_all_notebooks(FIXTURE_DIR, out_dir)

        assert len(results) == 5
        assert {r["keyword_zh"] for r in results} == EXPECTED_KEYWORDS
        for r in results:
            assert r["success"], r["error"]
            assert r["error"] is None
            assert r["keyword_id"] == compute_keyword_id(r["keyword_zh"])
            out_path = Path(r["output"])
            assert out_path.parent == out_dir
            assert out_path.name == (
                f"{r['keyword_zh']}__{r['keyword_id'][:8]}.json"
            )
            v4 = json.loads(out_path.read_text(encoding="utf-8"))
            assert v4["schema_version"] == "4.0"
            validate_notebook(v4)
            readiness = validate_discovery_readiness(v4)
            assert readiness.ready, readiness.errors
            active = [
                q for q in v4["search_queries"].values() if q["active"]
            ]
            assert r["active_queries"] == len(active)
            assert r["lane_count"] == len(active) * 4

    def test_invalid_notebook_recorded_as_failed_entry(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        valid = _fixture_paths()[0]
        (src / valid.name).write_text(
            valid.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (src / "broken.json").write_text(
            (INVALID_FIXTURE_DIR / "keyword_id_mismatch.json").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )

        results = migrate_all_notebooks(src, tmp_path / "out")
        assert len(results) == 2
        by_source = {Path(r["source"]).name: r for r in results}

        ok = by_source[valid.name]
        assert ok["success"] and ok["error"] is None

        bad = by_source["broken.json"]
        assert bad["success"] is False
        assert "LegacyNotebookContractError" in bad["error"]
        assert "does not match keyword_zh" in bad["error"]
        # The failing notebook must not leave any output behind.
        assert not (tmp_path / "out" / "broken.json").exists()
        outputs = list((tmp_path / "out").glob("*.json"))
        assert [p.name for p in outputs] == [valid.name]

    def test_v4_input_is_rejected(self, tmp_path):
        """Regression: migration input must be schema 3.0, not v4."""
        src = tmp_path / "src"
        src.mkdir()
        raw = _load_fixture(_fixture_paths()[0])
        raw["schema_version"] = "4.0"
        (src / "already_v4.json").write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8"
        )
        results = migrate_all_notebooks(src, tmp_path / "out")
        assert len(results) == 1
        assert results[0]["success"] is False
        assert "LegacyNotebookContractError" in results[0]["error"]

    def test_corrupt_json_recorded_as_failed_entry(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "corrupt.json").write_text("{not json", encoding="utf-8")
        results = migrate_all_notebooks(src, tmp_path / "out")
        assert len(results) == 1
        assert results[0]["success"] is False
        assert results[0]["error"]
