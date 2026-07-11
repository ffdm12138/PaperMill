import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import scripts.discover_papers_concurrent as mod


pytestmark = pytest.mark.unit


def _argv(*args: str) -> list[str]:
    return list(args)


def _fake_batch(statuses: list[str], *, exit_code: int = 0):
    keywords = [SimpleNamespace(keyword=f"kw{i}", status=status) for i, status in enumerate(statuses)]
    aggregate = {
        "keywords": {
            "total": len(statuses),
            "success": statuses.count("success"),
            "partial_success": statuses.count("partial_success"),
            "failed": statuses.count("failed"),
            "skipped": statuses.count("skipped"),
            "exhausted": statuses.count("exhausted"),
        },
        "refresh": {"pages_requested": 0, "pages_recovered": 0, "pages_persisted": 0, "items_returned": 0, "provider_failures": 0},
        "backfill": {"pages_requested": 0, "pages_recovered": 0, "pages_persisted": 0, "pages_committed": 0, "states_exhausted": 0, "provider_failures": 0},
        "pending": {"processed": 0, "remaining": 0, "backpressure": 0},
        "candidates": {"staged": 0, "emitted": 0, "existing_duplicates": 0, "duplicate_observations": 0, "invalid": 0, "unresolved": 0, "retryable_failures": 0},
        "budget": {"page_limit": None, "pages_used": 0, "page_budget_exhausted": False},
    }

    class _Batch:
        def __init__(self):
            self.status = "failed" if "failed" in statuses else ("partial_success" if "partial_success" in statuses else "success")
            self.keywords = keywords
            self.aggregate = aggregate
            self.exit_code = exit_code

        def to_dict(self):
            return {
                "schema_version": "3.0",
                "status": self.status,
                "exit_code": self.exit_code,
                "keywords": [{"keyword": kw.keyword, "status": kw.status} for kw in self.keywords],
                "aggregate": self.aggregate,
            }

    return _Batch()


def test_slugify_basic():
    assert mod._slugify("foo/bar baz!@#$") == "foo_bar_baz"
    assert len(mod._slugify("a" * 100)) == 60


def test_parse_args_queries_file(tmp_path: Path):
    qfile = tmp_path / "queries.txt"
    qfile.write_text("keyword 1\n# comment\nkeyword 2\n\n", encoding="utf-8")
    args = mod._parse_args(_argv("--queries-file", str(qfile)))
    assert args.queries == ["keyword 1", "keyword 2"]
    assert args.max_workers == 4


def test_parse_args_rejects_invalid_apply():
    with pytest.raises(SystemExit) as exc:
        mod._parse_args(_argv("--query", "t", "--apply"))
    assert exc.value.code == 2


def test_main_calls_single_coordinator_with_global_worker_cap(tmp_path: Path):
    report_dir = tmp_path / "reports"
    out_dir = tmp_path / "candidates"

    with patch.object(mod, "run_discovery_batch", return_value=_fake_batch(["success", "success"])) as run_batch:
        rc = mod.main_internal([
            "--query", "alpha",
            "--query", "beta",
            "--max-workers", "2",
            "--output-dir", str(out_dir),
            "--report-dir", str(report_dir),
        ])

    assert rc == 0
    queries, = run_batch.call_args.args
    assert queries == ["alpha", "beta"]
    assert run_batch.call_args.kwargs["max_workers"] == 2
    assert run_batch.call_args.kwargs["options"].output_dir.parent == out_dir


def test_main_writes_v3_batch_report(tmp_path: Path):
    report_dir = tmp_path / "reports"
    out_dir = tmp_path / "candidates"
    batch = _fake_batch(["success"])
    batch.aggregate["candidates"]["emitted"] = 2

    with patch.object(mod, "run_discovery_batch", return_value=batch):
        rc = mod.main_internal([
            "--query", "alpha",
            "--output-dir", str(out_dir),
            "--report-dir", str(report_dir),
        ])

    assert rc == 0
    report_files = list(report_dir.glob("concurrent_discovery_*.json"))
    assert len(report_files) == 1
    report = json.loads(report_files[0].read_text(encoding="utf-8"))
    assert report["schema_version"] == "3.0"
    assert report["batch_report"]["schema_version"] == "3.0"
    assert report["stage_summary"]["emitted"] == 2


def test_legacy_subprocess_helpers_are_removed():
    assert not hasattr(mod, "DISCOVER_SCRIPT")
    assert not hasattr(mod, "_build_command")
    assert not hasattr(mod, "_run_one")
