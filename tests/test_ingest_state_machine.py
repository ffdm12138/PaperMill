"""Tests for the centralized ingest state-machine constants + helper."""
from __future__ import annotations

import json
from pathlib import Path

from src.services import ingest_state
from src.services.ingest_state import (
    CATALOG_READY,
    COMMIT_FAILED,
    COMMITTED,
    FORMALIZE_FAILED,
    IMPORTED,
    METADATA_WARNINGS,
    POSSIBLE_DUPLICATE,
    READY_FOR_COMMIT,
    STUCK_HYGIENE_STATUSES,
    TERMINAL_READY_STATUSES,
    read_import_status,
    write_import_status,
)


def test_status_constants_match_existing_strings():
    # Regression: these literal strings are read by commit/curate/validate
    # and asserted by existing tests; renaming them silently breaks the gate.
    assert READY_FOR_COMMIT == "ready_for_commit"
    assert CATALOG_READY == "catalog_ready"
    assert FORMALIZE_FAILED == "formalize_failed"
    assert COMMIT_FAILED == "commit_failed"
    assert COMMITTED == "committed"
    assert IMPORTED == "imported"
    assert POSSIBLE_DUPLICATE == "possible_duplicate"
    assert METADATA_WARNINGS == "metadata_warnings"
    assert ingest_state.CONVERTED == "converted"
    assert ingest_state.STALE_CONVERSION == "stale_conversion"
    assert ingest_state.PARTIAL_CONVERSION == "partial_conversion"
    assert ingest_state.METADATA_CANDIDATES_FOUND == "metadata_candidates_found"
    assert ingest_state.METADATA_MANUAL_REVIEW_REQUIRED == "metadata_manual_review_required"


def test_terminal_ready_statuses_only_ready_for_commit():
    assert TERMINAL_READY_STATUSES == {"ready_for_commit"}


def test_stuck_hygiene_statuses_contains_new_states():
    for status in (CATALOG_READY, FORMALIZE_FAILED, COMMIT_FAILED):
        assert status in STUCK_HYGIENE_STATUSES
    # legacy resolver parked states remain stuck
    assert "metadata_candidates_found" in STUCK_HYGIENE_STATUSES
    assert "metadata_manual_review_required" in STUCK_HYGIENE_STATUSES


def test_write_import_status_writes_expected_fields(tmp_path: Path):
    folder = tmp_path / "000001"
    folder.mkdir()
    payload = write_import_status(
        folder,
        READY_FOR_COMMIT,
        reason="formalized",
        warnings=["w1"],
        extra={"paper_id": "2024_Wang_可信论文", "paper_number": "0000000000000001"},
    )
    assert payload["status"] == "ready_for_commit"
    assert payload["reason"] == "formalized"
    assert payload["errors"] == []
    assert payload["warnings"] == ["w1"]
    assert payload["paper_id"] == "2024_Wang_可信论文"
    assert payload["paper_number"] == "0000000000000001"
    assert "created_at" in payload

    on_disk = json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))
    assert on_disk == payload


def test_read_import_status_returns_empty_when_absent(tmp_path: Path):
    assert read_import_status(tmp_path) == {}


def test_read_import_status_returns_json_invalid_on_bad_json(tmp_path: Path):
    (tmp_path / ".import_status.json").write_text("{not json", encoding="utf-8")
    assert read_import_status(tmp_path) == {"status": "json_invalid"}
