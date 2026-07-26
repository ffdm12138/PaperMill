"""Unit tests for PendingCandidateStoreV4."""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from src.discovery.contracts.candidate import PendingCandidateV4
from src.discovery.stores.pending_candidate_store import (
    CandidateIdentityCollisionError,
    PendingCandidateCorruptError,
    PendingCandidateStoreV4,
)

pytestmark = pytest.mark.unit


def _store(root: Path) -> PendingCandidateStoreV4:
    workspace = types.SimpleNamespace(pending_candidates_dir=root / "pending_candidates")
    return PendingCandidateStoreV4(workspace)


def test_write_then_strict_read_roundtrip(tmp_path: Path):
    store = _store(tmp_path)
    candidate = PendingCandidateV4(
        candidate_id="c1",
        keyword_id="kw1",
        origin="legacy_candidate_seed",
        source_page_id="p1",
        doi="10.5555/one",
        normalized_doi="10.5555/one",
        title="Title",
        authors=["Au"],
        year=2020,
        venue="Venue",
        raw_provider_data={"provider": "openalex"},
        created_at="2026-07-24T00:00:00+00:00",
    )
    path = store.write(candidate)
    assert path == tmp_path / "pending_candidates" / "kw1" / "c1.json"

    loaded = store.read("kw1", "c1")
    assert loaded == candidate


def test_count_ignores_root_stray_and_deeper_json(tmp_path: Path):
    store = _store(tmp_path)
    store.write(PendingCandidateV4(
        candidate_id="c1", keyword_id="kw1", origin="manual_import",
    ))
    root = store.root_dir
    (root / "stray.json").write_text("[]", encoding="utf-8")
    nested = root / "kw1" / "nested"
    nested.mkdir(parents=True)
    (nested / "deep.json").write_text("{}", encoding="utf-8")

    assert store.count() == 1


def _candidate(candidate_id: str = "c1", keyword_id: str = "kw1",
               title: str = "Title") -> PendingCandidateV4:
    return PendingCandidateV4(
        candidate_id=candidate_id,
        keyword_id=keyword_id,
        origin="legacy_candidate_seed",
        source_page_id="p1",
        doi="10.5555/one",
        normalized_doi="10.5555/one",
        title=title,
        authors=["Au"],
        year=2020,
        venue="Venue",
        raw_provider_data={"provider": "openalex"},
        created_at="2026-07-24T00:00:00+00:00",
    )


def test_write_identical_payload_is_idempotent(tmp_path: Path):
    store = _store(tmp_path)
    first = store.write(_candidate())
    second = store.write(_candidate())
    assert first == second
    assert store.count() == 1


def test_write_conflicting_payload_raises_collision(tmp_path: Path):
    store = _store(tmp_path)
    store.write(_candidate(title="Original"))
    with pytest.raises(CandidateIdentityCollisionError):
        store.write(_candidate(title="Different candidate, same identity"))
    # The original payload is untouched.
    loaded = store.read("kw1", "c1")
    assert loaded is not None
    assert loaded.title == "Original"


def test_read_missing_returns_none(tmp_path: Path):
    store = _store(tmp_path)
    assert store.read("kw1", "absent") is None


def test_read_corrupt_json_raises_typed_error(tmp_path: Path):
    store = _store(tmp_path)
    path = tmp_path / "pending_candidates" / "kw1"
    path.mkdir(parents=True)
    (path / "c1.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(PendingCandidateCorruptError):
        store.read("kw1", "c1")


def test_read_non_object_raises_typed_error(tmp_path: Path):
    store = _store(tmp_path)
    path = tmp_path / "pending_candidates" / "kw1"
    path.mkdir(parents=True)
    (path / "c1.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(PendingCandidateCorruptError):
        store.read("kw1", "c1")


def test_read_schema_violation_raises_typed_error(tmp_path: Path):
    store = _store(tmp_path)
    path = tmp_path / "pending_candidates" / "kw1"
    path.mkdir(parents=True)
    (path / "c1.json").write_text('{"surprise": true}', encoding="utf-8")
    with pytest.raises(PendingCandidateCorruptError):
        store.read("kw1", "c1")
