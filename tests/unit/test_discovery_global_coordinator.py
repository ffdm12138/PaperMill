from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from src.discovery.coordinator import DiscoveryOptions, run_discovery_batch
from src.discovery.models import PaperCandidate


pytestmark = pytest.mark.unit


@dataclass
class _Page:
    candidates: list[PaperCandidate]
    next_cursor: str | None = None
    exhausted: bool = True
    status: str = "success"
    safe_error: str | None = None
    error_type: str | None = None

    @property
    def returned_count(self) -> int:
        return len(self.candidates)


def test_global_page_budget_counts_network_requests(tmp_path: Path):
    calls: list[str] = []

    def fetch(provider: str, query: str, **kwargs):
        calls.append(f"{provider}:{kwargs['lane']}:{kwargs['cursor']}")
        return _Page([PaperCandidate(title="T", doi=f"10.1234/{len(calls)}")], next_cursor=f"C{len(calls)}", exhausted=False)

    options = DiscoveryOptions(
        mode="backfill",
        refresh_pages=2,
        backfill_pages=2,
        max_pages_total=2,
        max_candidates=10,
        notebook_dir=tmp_path / "notebooks",
        pending_pages_dir=tmp_path / "pages",
        locks_dir=tmp_path / "locks",
        exports_dir=tmp_path / "exports",
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
    )
    report = run_discovery_batch(["kw"], options=options, max_workers=4, fetch_page=fetch)
    assert len(calls) == 2
    assert report.exit_code == 0
    assert report.status == "success"
    assert report.aggregate["budget"]["pages_used"] == 2
    assert report.aggregate["budget"]["page_budget_exhausted"] is True
    assert report.aggregate["backfill"]["provider_failures"] == 0
    assert report.keywords[0].backfill.errors == []
    assert report.keywords[0].backfill.stop_reason == "page_budget_exhausted"


def test_report_aggregation_uses_in_memory_objects(tmp_path: Path):
    options = DiscoveryOptions(
        mode="refresh",
        refresh_pages=1,
        max_candidates=5,
        notebook_dir=tmp_path / "notebooks",
        pending_pages_dir=tmp_path / "pages",
        locks_dir=tmp_path / "locks",
        exports_dir=tmp_path / "exports",
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
    )
    report = run_discovery_batch(["alpha", "beta"], options=options, max_workers=2, fetch_page=lambda *a, **k: _Page([]))
    assert report.to_dict()["schema_version"] == "3.0"
    assert report.aggregate["keywords"]["total"] == 2
    assert len(list((tmp_path / "pages").glob("**/*.json"))) >= 2
