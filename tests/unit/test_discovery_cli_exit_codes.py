from __future__ import annotations

from unittest.mock import patch

import pytest

import scripts.discover_papers as discover_script
import scripts.discover_papers_concurrent as concurrent_script
from src.discovery.keyword_notebook import KeywordNotebookStore, keyword_id
from tests.helpers.relevance_profiles import bind_test_relevance_profile


pytestmark = pytest.mark.unit


def _seed_ready(root, keyword_zh: str) -> None:
    store = KeywordNotebookStore(root)
    store.ensure_notebook(keyword_zh)
    store.sync_search_queries(
        keyword_zh,
        add=[
            {"query": keyword_zh, "language": "zh", "source": "test"},
            {"query": f"english topic {keyword_id(keyword_zh)}", "language": "en", "source": "test"},
        ],
        operator="test",
    )
    bind_test_relevance_profile(store, keyword_zh)
    store.set_enabled(keyword_zh, True)


class _Keyword:
    def __init__(self, keyword: str, status: str):
        self.keyword_zh = keyword
        self.status = status

    def to_dict(self):
        return {
            "schema_version": "3.0",
            "keyword_zh": self.keyword_zh,
            "status": self.status,
            "mode": "hybrid",
            "refresh": {
                "status": "partial_success",
                "pages_requested": 0,
                "pages_recovered": 0,
                "items_returned": 0,
                "provider_failures": 1,
            },
            "backfill": {
                "status": "skipped",
                "pages_requested": 0,
                "pages_recovered": 0,
                "pages_committed": 0,
                "states_exhausted": 0,
                "provider_failures": 0,
            },
            "candidates": {
                "staged": 0,
                "emitted": 0,
                "existing_duplicates": 0,
                "duplicate_observations": 0,
            },
        }


class _Batch:
    def __init__(self, keywords: list[str], status: str, exit_code: int):
        self.status = status
        self.exit_code = exit_code
        self.keywords = [_Keyword(keyword, status) for keyword in keywords]
        self.aggregate = {
            "keywords": {
                "total": len(keywords),
                "success": int(status == "success") * len(keywords),
                "partial_success": int(status == "partial_success") * len(keywords),
                "failed": int(status == "failed") * len(keywords),
                "skipped": 0,
            },
            "refresh": {"pages_requested": 0, "pages_recovered": 0, "provider_failures": 1},
            "backfill": {"pages_requested": 0, "pages_recovered": 0, "states_exhausted": 0, "provider_failures": 0},
            "candidates": {"staged": 0, "emitted": 0, "existing_duplicates": 0, "duplicate_observations": 0},
        }

    def to_dict(self):
        return {
            "schema_version": "3.0",
            "status": self.status,
            "exit_code": self.exit_code,
            "keywords": [],
            "aggregate": self.aggregate,
        }


def test_single_keyword_cli_returns_coordinator_exit_code(tmp_path):
    notebooks = tmp_path / "notebooks"
    _seed_ready(notebooks, "风吹雪")
    with patch.object(
        discover_script,
        "run_discovery_batch",
        return_value=_Batch(["风吹雪"], "partial_success", 2),
    ):
        rc = discover_script.main([
            "--keyword-zh", "风吹雪",
            "--keyword-notebook-dir", str(notebooks),
            "--output-dir", str(tmp_path / "out"),
            "--pending-pages-dir", str(tmp_path / "pages"),
            "--discovery-locks-dir", str(tmp_path / "locks"),
            "--exports-dir", str(tmp_path / "exports"),
            "--paper-raw-dir", str(tmp_path / "paper_raw"),
            "--papers-dir", str(tmp_path / "papers"),
            "--ledger-path", str(tmp_path / "ledger.json"),
        ])
    assert rc == 2


def test_multi_keyword_cli_returns_coordinator_exit_code(tmp_path):
    notebooks = tmp_path / "notebooks"
    keywords = ["风吹雪", "雪粒破碎", "风洞实验"]
    for keyword in keywords:
        _seed_ready(notebooks, keyword)
    argv = sum((["--keyword-zh", value] for value in keywords), [])
    with patch.object(
        concurrent_script,
        "run_discovery_batch",
        return_value=_Batch(keywords, "failed", 1),
    ):
        rc = concurrent_script.main_internal([
            *argv,
            "--keyword-notebook-dir", str(notebooks),
            "--output-dir", str(tmp_path / "out"),
            "--report-dir", str(tmp_path / "reports"),
            "--pending-pages-dir", str(tmp_path / "pages"),
            "--discovery-locks-dir", str(tmp_path / "locks"),
            "--exports-dir", str(tmp_path / "exports"),
            "--paper-raw-dir", str(tmp_path / "paper_raw"),
            "--papers-dir", str(tmp_path / "papers"),
            "--ledger-path", str(tmp_path / "ledger.json"),
        ])
    assert rc == 1


def test_disabled_single_notebook_is_only_successful_skip(tmp_path):
    notebooks = tmp_path / "notebooks"
    _seed_ready(notebooks, "风吹雪")
    KeywordNotebookStore(notebooks).set_enabled("风吹雪", False)
    with patch.object(discover_script, "run_discovery_batch") as run_batch:
        rc = discover_script.main([
            "--keyword-zh", "风吹雪",
            "--keyword-notebook-dir", str(notebooks),
        ])
    assert rc == 0
    run_batch.assert_not_called()


def test_missing_single_notebook_fails_without_provider_call(tmp_path):
    with patch.object(discover_script, "run_discovery_batch") as run_batch:
        rc = discover_script.main([
            "--keyword-zh", "风吹雪",
            "--keyword-notebook-dir", str(tmp_path / "missing"),
            "--dry-run",
        ])
    assert rc == 1
    run_batch.assert_not_called()
