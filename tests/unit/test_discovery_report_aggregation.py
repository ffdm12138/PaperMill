from __future__ import annotations

import pytest

from src.discovery.coordinator import BatchDiscoveryReport, KeywordDiscoveryReport, LaneReport, PageBudget, _aggregate
from src.discovery.pending_queue import DrainReport


pytestmark = pytest.mark.unit


def test_batch_report_serializes_schema_v3_without_rereading_files():
    drain = DrainReport(processed=1, emitted=1)
    keyword = KeywordDiscoveryReport(
        keyword="kw",
        keyword_id="kid",
        status="success",
        refresh=LaneReport(status="success", pages_requested=1, items_returned=1),
        backfill=LaneReport(status="skipped"),
        pending=DrainReport(),
        final_pending=drain,
        candidates={"emitted": 1},
        budget={"page_limit": None, "pages_used": 1},
        mode="refresh",
    )
    aggregate = _aggregate([keyword], PageBudget(10, used=1))
    batch = BatchDiscoveryReport(status="success", keywords=[keyword], aggregate=aggregate, exit_code=0)
    data = batch.to_dict()
    assert data["schema_version"] == "3.0"
    assert data["aggregate"]["candidates"]["emitted"] == 1
    assert data["keywords"][0]["refresh"]["pages_requested"] == 1
