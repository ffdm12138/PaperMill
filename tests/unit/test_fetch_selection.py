"""Tests for batch selection of PDF fetch candidates.

Eligibility says a workspace *can* be fetched; selection says this run
*should* spend time on it.  Without selection, re-running the backlog replays
every known-hard failure before reaching any never-attempted workspace.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.fetch.access_policy import (
    FetchSelection,
    classify_pdf_fetch_candidate,
    last_fetch_attempt_at,
    select_fetch_candidates,
)
from src.metadata.schema import empty_metadata


NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)


def _workspace(root, paper_number: str, doi: str, *, attempted_at: datetime | None = None,
               with_pdf: bool = False):
    folder = root / paper_number
    folder.mkdir(parents=True, exist_ok=True)
    metadata = empty_metadata(paper_number, source_type="network_search")
    metadata["identifiers"]["doi"] = doi
    (folder / f"{paper_number}.metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    if with_pdf:
        (folder / f"{paper_number}.pdf").write_bytes(b"%PDF-1.4 test")
    if attempted_at is not None:
        records = folder / "source_records"
        records.mkdir(exist_ok=True)
        (records / "fetch_result.json").write_text(
            json.dumps({"fetch_result": {"success": False, "fetched_at": attempted_at.isoformat()}}),
            encoding="utf-8")
    return folder


def _classify(root, numbers):
    return [classify_pdf_fetch_candidate(root / n, n) for n in numbers]


def _selected(candidates):
    return [c.paper_number for c in candidates if c.eligible]


# ── 1. no filters selects everything eligible ─────────────────────────

def test_noop_selection_changes_nothing(tmp_path):
    _workspace(tmp_path, "0" * 15 + "1", "10.5194/acp-1-1-2020")
    _workspace(tmp_path, "0" * 15 + "2", "10.3390/su18031645", attempted_at=NOW)
    candidates = select_fetch_candidates(
        _classify(tmp_path, ["0" * 15 + "1", "0" * 15 + "2"]), FetchSelection(), now=NOW)
    assert _selected(candidates) == ["0" * 15 + "1", "0" * 15 + "2"]
    assert FetchSelection().is_noop is True


# ── 2. --skip-attempted reaches never-attempted work ──────────────────

def test_skip_attempted_excludes_workspaces_with_a_fetch_sidecar(tmp_path):
    fresh, tried = "0" * 15 + "1", "0" * 15 + "2"
    _workspace(tmp_path, fresh, "10.5194/acp-1-1-2020")
    _workspace(tmp_path, tried, "10.5194/acp-2-2-2020", attempted_at=NOW - timedelta(days=1))
    candidates = select_fetch_candidates(
        _classify(tmp_path, [fresh, tried]), FetchSelection(skip_attempted=True), now=NOW)
    assert _selected(candidates) == [fresh]
    assert next(c for c in candidates if c.paper_number == tried).reason == "already attempted"


# ── 3. --retry-after-days ─────────────────────────────────────────────

@pytest.mark.parametrize("age_days,expected_selected", [(1, False), (45, True)])
def test_retry_after_days_respects_attempt_age(tmp_path, age_days, expected_selected):
    number = "0" * 15 + "1"
    _workspace(tmp_path, number, "10.5194/acp-1-1-2020",
               attempted_at=NOW - timedelta(days=age_days))
    candidates = select_fetch_candidates(
        _classify(tmp_path, [number]), FetchSelection(retry_after_days=30), now=NOW)
    assert bool(_selected(candidates)) is expected_selected


def test_retry_after_days_always_selects_never_attempted(tmp_path):
    number = "0" * 15 + "1"
    _workspace(tmp_path, number, "10.5194/acp-1-1-2020")
    candidates = select_fetch_candidates(
        _classify(tmp_path, [number]), FetchSelection(retry_after_days=30), now=NOW)
    assert _selected(candidates) == [number]


# ── 4. --doi-prefix ───────────────────────────────────────────────────

def test_doi_prefix_filters_by_registrant(tmp_path):
    keep, drop = "0" * 15 + "1", "0" * 15 + "2"
    _workspace(tmp_path, keep, "10.5194/acp-1-1-2020")
    _workspace(tmp_path, drop, "10.3390/su18031645")
    candidates = select_fetch_candidates(
        _classify(tmp_path, [keep, drop]), FetchSelection(doi_prefixes=("10.5194",)), now=NOW)
    assert _selected(candidates) == [keep]
    assert next(c for c in candidates if c.paper_number == drop).reason == "DOI prefix not selected"


def test_doi_prefix_tolerates_trailing_slash_and_case(tmp_path):
    number = "0" * 15 + "1"
    _workspace(tmp_path, number, "10.5194/acp-1-1-2020")
    candidates = select_fetch_candidates(
        _classify(tmp_path, [number]), FetchSelection(doi_prefixes=(" 10.5194/ ",)), now=NOW)
    assert _selected(candidates) == [number]


# ── 5. --limit bounds the batch, applied after other filters ──────────

def test_limit_bounds_the_batch(tmp_path):
    numbers = [f"{i:016d}" for i in range(1, 6)]
    for number in numbers:
        _workspace(tmp_path, number, f"10.5194/acp-{number}-2020")
    candidates = select_fetch_candidates(
        _classify(tmp_path, numbers), FetchSelection(limit=2), now=NOW)
    assert len(_selected(candidates)) == 2
    assert candidates[-1].reason == "beyond --limit for this batch"


def test_limit_counts_only_survivors_of_earlier_filters(tmp_path):
    wrong, right_a, right_b = "0" * 15 + "1", "0" * 15 + "2", "0" * 15 + "3"
    _workspace(tmp_path, wrong, "10.3390/su18031645")
    _workspace(tmp_path, right_a, "10.5194/acp-1-1-2020")
    _workspace(tmp_path, right_b, "10.5194/acp-2-2-2020")
    candidates = select_fetch_candidates(
        _classify(tmp_path, [wrong, right_a, right_b]),
        FetchSelection(doi_prefixes=("10.5194",), limit=2), now=NOW)
    assert _selected(candidates) == [right_a, right_b]


# ── 6. selection never revives an ineligible workspace ────────────────

def test_selection_does_not_override_eligibility(tmp_path):
    number = "0" * 15 + "1"
    _workspace(tmp_path, number, "10.5194/acp-1-1-2020", with_pdf=True)
    candidates = select_fetch_candidates(
        _classify(tmp_path, [number]), FetchSelection(doi_prefixes=("10.5194",)), now=NOW)
    assert _selected(candidates) == []
    assert candidates[0].reason == "PDF already exists"


# ── 7. attempt timestamps ─────────────────────────────────────────────

def test_last_attempt_reads_the_sidecar_timestamp(tmp_path):
    number = "0" * 15 + "1"
    stamp = NOW - timedelta(days=3)
    folder = _workspace(tmp_path, number, "10.5194/acp-1-1-2020", attempted_at=stamp)
    assert last_fetch_attempt_at(folder) == stamp


def test_last_attempt_falls_back_to_mtime_when_timestamp_missing(tmp_path):
    number = "0" * 15 + "1"
    folder = _workspace(tmp_path, number, "10.5194/acp-1-1-2020")
    records = folder / "source_records"
    records.mkdir(exist_ok=True)
    (records / "fetch_result.json").write_text(
        json.dumps({"fetch_result": {"success": False}}), encoding="utf-8")
    assert last_fetch_attempt_at(folder) is not None


def test_last_attempt_is_none_without_a_sidecar(tmp_path):
    number = "0" * 15 + "1"
    folder = _workspace(tmp_path, number, "10.5194/acp-1-1-2020")
    assert last_fetch_attempt_at(folder) is None
