"""Hygiene test: discovery final architecture verifier.

Runs the AST-based verifier and asserts zero forbidden patterns.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def test_discovery_final_architecture_passes() -> None:
    """The AST-based architecture verifier must report zero errors."""
    from scripts.verify_discovery_final_architecture import (
        verify_discovery_final_architecture,
    )

    report = verify_discovery_final_architecture()

    errors = report.errors
    if errors:
        msg_lines = [f"{len(errors)} architecture violation(s):"]
        for f in errors:
            msg_lines.append(f"  {f.file}:{f.line}: [{f.category}] {f.message}")
        pytest.fail("\n".join(msg_lines))

    # Report must have scanned at least the core discovery files
    scanned = set(report.files_scanned)
    required = {"src/discovery/coordinator.py", "src/discovery/lane_executor.py",
                "src/discovery/provider_client.py", "src/discovery/backfill_transaction.py",
                "src/discovery/keyword_notebook.py", "src/discovery/report_builder.py"}
    missing = required - scanned
    assert not missing, f"Verifier did not scan required files: {missing}"


def test_official_entrypoint_dispatches_typed_lanes_and_builds_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dynamic companion to the AST verifier.

    The public coordinator must schedule every physical lane through the typed
    executors and hand all outcomes to one final ``ReportBuilder.build`` call;
    no compatibility callback or per-lane report builder is allowed.
    """
    import src.discovery.coordinator as coordinator
    from src.discovery.coordinator import DiscoveryOptions, run_discovery_batch
    from src.discovery.keyword_notebook import KeywordNotebookStore
    from src.discovery.providers.provider_page_fetcher import CallbackProviderPageFetcher
    from src.discovery.reporting.report_builder import ReportBuilder as RealReportBuilder
    from tests.helpers.fake_provider import discovery_page
    from tests.helpers.relevance_profiles import bind_test_relevance_profile

    notebooks = tmp_path / "notebooks"
    store = KeywordNotebookStore(notebooks)
    store.ensure_notebook("风吹雪")
    store.sync_search_queries("风吹雪", add=[
        {"query": "风吹雪", "language": "zh"},
        {"query": "blowing snow", "language": "en"},
    ])
    bind_test_relevance_profile(store, "风吹雪")
    store.set_enabled("风吹雪", True)

    dispatched: list[tuple[str, str, str]] = []

    def fetch(spec, cursor, _client):
        dispatched.append((spec.key.mode, spec.key.provider, spec.key.query_id))
        return discovery_page(
            provider=spec.key.provider,
            keyword_zh=spec.keyword_zh,
            query=spec.query,
            lane=spec.key.mode,
            cursor=cursor,
            query_id=spec.key.query_id,
            query_language=spec.query_language,
            candidates=[],
            exhausted=True,
        )

    refresh_specs: list[object] = []
    backfill_specs: list[object] = []
    real_refresh = coordinator.execute_refresh_lane
    real_backfill = coordinator.execute_backfill_lane

    def spy_refresh(spec, **kwargs):
        refresh_specs.append(spec)
        return real_refresh(spec, **kwargs)

    def spy_backfill(spec, **kwargs):
        backfill_specs.append(spec)
        return real_backfill(spec, **kwargs)

    builds: list[object] = []

    class SpyReportBuilder:
        def __init__(self) -> None:
            self._delegate = RealReportBuilder()

        def build(self, **kwargs):
            builds.append(kwargs)
            return self._delegate.build(**kwargs)

    monkeypatch.setattr(coordinator, "execute_refresh_lane", spy_refresh)
    monkeypatch.setattr(coordinator, "execute_backfill_lane", spy_backfill)
    monkeypatch.setattr(coordinator, "ReportBuilder", SpyReportBuilder)

    options = DiscoveryOptions(
        mode="hybrid",
        refresh_pages=1,
        backfill_pages=1,
        max_candidates=8,
        notebook_dir=notebooks,
        pending_pages_dir=tmp_path / "pages",
        locks_dir=tmp_path / "locks",
        exports_dir=tmp_path / "exports",
        output_dir=tmp_path / "output",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
    )
    report = run_discovery_batch(
        ["风吹雪"],
        options=options,
        max_workers=1,
        page_fetcher=CallbackProviderPageFetcher(fetch),
    )

    assert report.exit_code == 0
    assert len(refresh_specs) == 4
    assert len(backfill_specs) == 4
    assert len({spec.key.stable_id() for spec in [*refresh_specs, *backfill_specs]}) == 8
    assert len(dispatched) == 8
    assert len(builds) == 1
