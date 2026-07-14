from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import pytest

from src.discovery.coordinator import DiscoveryOptions, run_discovery_batch
from src.discovery.keyword_notebook import KeywordNotebookStore
from src.discovery.models import PaperCandidate


pytestmark = pytest.mark.unit


def _seed_ready_notebook(store: KeywordNotebookStore, keyword_zh: str) -> None:
    """Create a v3 notebook with bilingual queries needed for discovery readiness."""
    store.ensure_notebook(keyword_zh)
    store.sync_search_queries(keyword_zh, add=[
        {"query": keyword_zh, "language": "zh"},
        {"query": "blowing snow", "language": "en"},
    ])
    store.set_enabled(keyword_zh, True)


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

    nb_dir = tmp_path / "notebooks"
    _seed_ready_notebook(KeywordNotebookStore(nb_dir), "风吹雪")

    options = DiscoveryOptions(
        mode="backfill", refresh_pages=2, backfill_pages=2,
        max_pages_total=2, max_candidates=10,
        notebook_dir=nb_dir,
        pending_pages_dir=tmp_path / "pages",
        locks_dir=tmp_path / "locks",
        exports_dir=tmp_path / "exports",
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
    )
    report = run_discovery_batch(["风吹雪"], options=options, max_workers=4, fetch_page=fetch)
    assert len(calls) == 2
    assert report.exit_code == 0
    assert report.status == "success"
    assert report.aggregate["budget"]["pages_used"] == 2
    assert report.aggregate["budget"]["page_budget_exhausted"] is True
    assert report.aggregate["backfill"]["provider_failures"] == 0
    assert report.keywords[0].backfill.errors == []
    assert report.keywords[0].backfill.stop_reason == "page_budget_exhausted"


def test_report_aggregation_uses_in_memory_objects(tmp_path: Path):
    nb_dir = tmp_path / "notebooks"
    store = KeywordNotebookStore(nb_dir)
    for kw in ("主题甲", "主题乙"):
        _seed_ready_notebook(store, kw)

    options = DiscoveryOptions(
        mode="refresh", refresh_pages=1, max_candidates=5,
        notebook_dir=nb_dir,
        pending_pages_dir=tmp_path / "pages",
        locks_dir=tmp_path / "locks",
        exports_dir=tmp_path / "exports",
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
    )
    report = run_discovery_batch(["主题甲", "主题乙"], options=options, max_workers=2, fetch_page=lambda *a, **k: _Page([]))
    assert report.to_dict()["schema_version"] == "3.0"
    assert report.aggregate["keywords"]["total"] == 2
    assert len(list((tmp_path / "pages").glob("**/*.json"))) >= 2


@pytest.mark.parametrize("state", ["missing", "not_ready", "corrupt", "legacy"])
def test_invalid_or_unready_notebook_fails_closed_without_provider_calls(tmp_path: Path, state: str):
    nb_dir = tmp_path / "notebooks"
    store = KeywordNotebookStore(nb_dir)
    if state in {"not_ready", "corrupt", "legacy"}:
        store.ensure_notebook("风吹雪")
    if state == "not_ready":
        store.sync_search_queries("风吹雪", add=[{"query": "风吹雪", "language": "zh"}])
        path = next(nb_dir.glob("*.json"))
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["enabled"] = True
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    elif state in {"corrupt", "legacy"}:
        path = next(nb_dir.glob("*.json"))
        if state == "corrupt":
            path.write_text("{broken", encoding="utf-8")
        else:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["schema_version"] = "2.0"
            path.write_text(json.dumps(payload), encoding="utf-8")
    calls = []
    options = DiscoveryOptions(
        mode="refresh", refresh_pages=1, backfill_pages=1, max_candidates=0,
        notebook_dir=nb_dir, pending_pages_dir=tmp_path / "pages",
        locks_dir=tmp_path / "locks", exports_dir=tmp_path / "exports",
        output_dir=tmp_path / "out", paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers", ledger_path=tmp_path / "ledger.json",
    )
    report = run_discovery_batch(
        ["风吹雪"], options=options, max_workers=1,
        fetch_page=lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    assert calls == []
    assert report.status == "failed"
    assert report.exit_code == 1
    assert report.keywords[0].status == "failed"
    assert report.keywords[0].errors


def test_disabled_notebook_is_the_only_zero_exit_skip(tmp_path: Path):
    nb_dir = tmp_path / "notebooks"
    store = KeywordNotebookStore(nb_dir)
    store.ensure_notebook("风吹雪")
    store.set_enabled("风吹雪", False)
    options = DiscoveryOptions(
        mode="refresh", refresh_pages=1, backfill_pages=1, max_candidates=0,
        notebook_dir=nb_dir, pending_pages_dir=tmp_path / "pages",
        locks_dir=tmp_path / "locks", exports_dir=tmp_path / "exports",
        output_dir=tmp_path / "out", paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers", ledger_path=tmp_path / "ledger.json",
    )
    report = run_discovery_batch(["风吹雪"], options=options, max_workers=1, fetch_page=lambda *a, **k: pytest.fail("provider called"))
    assert report.status == "success"
    assert report.exit_code == 0
    assert report.keywords[0].status == "skipped"


def test_two_chinese_and_two_english_queries_schedule_both_providers(tmp_path: Path):
    nb_dir = tmp_path / "notebooks"
    store = KeywordNotebookStore(nb_dir)
    store.ensure_notebook("大气边界层")
    store.sync_search_queries("大气边界层", add=[
        {"query": "大气边界层", "language": "zh"},
        {"query": "边界层湍流", "language": "zh"},
        {"query": "atmospheric boundary layer", "language": "en"},
        {"query": "boundary layer turbulence", "language": "en"},
    ])
    store.set_enabled("大气边界层", True)
    calls = []
    options = DiscoveryOptions(
        mode="refresh", refresh_pages=1, backfill_pages=1, max_candidates=0,
        notebook_dir=nb_dir, pending_pages_dir=tmp_path / "pages",
        locks_dir=tmp_path / "locks", exports_dir=tmp_path / "exports",
        output_dir=tmp_path / "out", paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers", ledger_path=tmp_path / "ledger.json",
    )
    def fetch(provider, query, **kwargs):
        calls.append((provider, query))
        return _Page([])
    report = run_discovery_batch(["大气边界层"], options=options, max_workers=2, fetch_page=fetch)
    assert report.exit_code == 0
    assert len(calls) == 8
    assert len(set(calls)) == 8
    assert sum(provider == "openalex" for provider, _query in calls) == 4
    assert sum(provider == "crossref" for provider, _query in calls) == 4
    keyword_report = report.keywords[0].to_dict()
    assert keyword_report["keyword_zh"] == "大气边界层"
    assert keyword_report["queries_total"] == 4
    assert keyword_report["queries_zh"] == 2
    assert keyword_report["queries_en"] == 2
    assert keyword_report["queries_executed"] == [
        {"query": "大气边界层", "query_language": "zh"},
        {"query": "边界层湍流", "query_language": "zh"},
        {"query": "atmospheric boundary layer", "query_language": "en"},
        {"query": "boundary layer turbulence", "query_language": "en"},
    ]
