from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import scripts.discover_papers as discover_script
import scripts.discover_papers_concurrent as concurrent_script


pytestmark = pytest.mark.unit


class _Keyword:
    def __init__(self, keyword: str, status: str):
        self.keyword = keyword
        self.status = status

    def to_dict(self):
        return {
            "schema_version": "3.0",
            "keyword": self.keyword,
            "status": self.status,
            "mode": "hybrid",
            "refresh": {"status": "success", "pages_requested": 0, "pages_recovered": 0, "items_returned": 0, "provider_failures": 0},
            "backfill": {"status": "skipped", "pages_requested": 0, "pages_recovered": 0, "pages_committed": 0, "states_exhausted": 0, "provider_failures": 0},
            "candidates": {"staged": 0, "emitted": 0, "existing_duplicates": 0, "duplicate_observations": 0, "invalid": 0, "unresolved": 0},
            "errors": [],
        }


class _Batch:
    def __init__(self, status: str, exit_code: int):
        self.status = status
        self.exit_code = exit_code
        self.aggregate = {
            "keywords": {"total": 1, "success": int(status == "success"), "partial_success": int(status == "partial_success"), "failed": int(status == "failed"), "skipped": 0},
            "refresh": {"pages_requested": 0, "pages_recovered": 0, "provider_failures": 0},
            "backfill": {"pages_requested": 0, "pages_recovered": 0, "states_exhausted": 0, "provider_failures": 0},
            "candidates": {"staged": 0, "emitted": 0, "existing_duplicates": 0, "duplicate_observations": 0},
        }
        self.keywords = [_Keyword("kw", status)]

    def to_dict(self):
        return {"schema_version": "3.0", "status": self.status, "exit_code": self.exit_code, "keywords": []}


def test_single_keyword_cli_returns_coordinator_exit_code(tmp_path):
    with patch.object(discover_script, "run_discovery_batch", return_value=_Batch("partial_success", 2)):
        argv = [
            "discover_papers.py",
            "kw",
            "--output-dir", str(tmp_path / "out"),
            "--keyword-notebook-dir", str(tmp_path / "notebooks"),
            "--pending-pages-dir", str(tmp_path / "pages"),
            "--discovery-locks-dir", str(tmp_path / "locks"),
            "--exports-dir", str(tmp_path / "exports"),
            "--paper-raw-dir", str(tmp_path / "paper_raw"),
            "--papers-dir", str(tmp_path / "papers"),
            "--ledger-path", str(tmp_path / "ledger.json"),
        ]
        with patch.object(discover_script.sys, "argv", argv):
            rc = discover_script.main()
    assert rc == 2


def test_multi_keyword_cli_returns_coordinator_exit_code(tmp_path):
    with patch.object(concurrent_script, "run_discovery_batch", return_value=_Batch("failed", 1)):
        rc = concurrent_script.main_internal([
            "--query", "kw",
            "--output-dir", str(tmp_path / "out"),
            "--log-dir", str(tmp_path / "logs"),
            "--report-dir", str(tmp_path / "reports"),
            "--keyword-notebook-dir", str(tmp_path / "notebooks"),
            "--pending-pages-dir", str(tmp_path / "pages"),
            "--discovery-locks-dir", str(tmp_path / "locks"),
            "--exports-dir", str(tmp_path / "exports"),
            "--paper-raw-dir", str(tmp_path / "paper_raw"),
            "--papers-dir", str(tmp_path / "papers"),
            "--ledger-path", str(tmp_path / "ledger.json"),
        ])
    assert rc == 1
