"""Unit tests for scripts/discover_papers_concurrent.py wrapper."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import scripts.discover_papers_concurrent as mod


pytestmark = pytest.mark.unit


def _argv(*args: str) -> list[str]:
    return list(args)


# ---------------------------------------------------------------------------
# _slugify
# ---------------------------------------------------------------------------


class TestSlugify:
    def test_basic(self):
        assert mod._slugify("hello world") == "hello_world"

    def test_special_chars(self):
        assert mod._slugify("foo/bar baz!@#$") == "foo_bar_baz"

    def test_truncate(self):
        long_str = "a" * 100
        assert len(mod._slugify(long_str)) == 60


# ---------------------------------------------------------------------------
# _parse_args
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_single_query(self):
        args = mod._parse_args(_argv("--query", "test query"))
        assert args.queries == ["test query"]
        assert args.max_workers == 4

    def test_multiple_queries(self):
        args = mod._parse_args(_argv("--query", "q1", "--query", "q2"))
        assert args.queries == ["q1", "q2"]

    def test_queries_file(self, tmp_path: Path):
        qfile = tmp_path / "queries.txt"
        qfile.write_text("keyword 1\n# comment\nkeyword 2\n\nkeyword 3\n", encoding="utf-8")
        args = mod._parse_args(_argv("--queries-file", str(qfile)))
        assert args.queries == ["keyword 1", "keyword 2", "keyword 3"]

    def test_max_workers_default(self):
        args = mod._parse_args(_argv("--query", "test"))
        assert args.max_workers == 4

    def test_rejects_no_queries(self):
        with pytest.raises(SystemExit) as exc:
            mod._parse_args([])
        assert exc.value.code == 2

    def test_rejects_max_workers_zero(self):
        with pytest.raises(SystemExit) as exc:
            mod._parse_args(_argv("--query", "t", "--max-workers", "0"))
        assert exc.value.code == 2

    def test_rejects_max_candidates_zero(self):
        with pytest.raises(SystemExit) as exc:
            mod._parse_args(_argv("--query", "t", "--max-candidates", "0"))
        assert exc.value.code == 2

    def test_rejects_limit_per_query_zero(self):
        with pytest.raises(SystemExit) as exc:
            mod._parse_args(_argv("--query", "t", "--limit-per-query", "0"))
        assert exc.value.code == 2

    def test_rejects_apply_without_stage(self):
        with pytest.raises(SystemExit) as exc:
            mod._parse_args(_argv("--query", "t", "--apply"))
        assert exc.value.code == 2

    def test_rejects_skip_duplicates_without_stage(self):
        with pytest.raises(SystemExit) as exc:
            mod._parse_args(_argv("--query", "t", "--skip-duplicates"))
        assert exc.value.code == 2

    def test_hide_existing_pass_through_no_staging_required(self):
        """--hide-existing is query-phase, no --stage-to-paper-raw required."""
        args = mod._parse_args(_argv("--query", "t", "--hide-existing"))
        assert args.hide_existing is True

    def test_queries_file_not_found(self, tmp_path: Path):
        with pytest.raises(SystemExit) as exc:
            mod._parse_args(_argv("--queries-file", str(tmp_path / "nonexistent.txt")))
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# _build_command
# ---------------------------------------------------------------------------


class TestBuildCommand:
    def test_uses_absolute_script_path(self):
        args = mod._parse_args(_argv("--query", "test"))
        cmd, _, _, _ = mod._build_command("test", 0, "20260707_120000_123456", args)
        assert cmd[0] == subprocess.sys.executable
        assert str(mod.DISCOVER_SCRIPT) in cmd[1]

    def test_passes_per_query_output_dir(self):
        args = mod._parse_args(_argv("--query", "test"))
        _, output_dir, _, _ = mod._build_command("test", 0, "20260707_120000_123456", args)
        assert "concurrent_20260707_120000_123456" in str(output_dir)
        assert "000_test" in str(output_dir)

    def test_passes_per_query_report_when_staging(self):
        args = mod._parse_args(_argv("--query", "test", "--stage-to-paper-raw"))
        _, _, report_path, _ = mod._build_command("test", 0, "20260707_120000_123456", args)
        assert report_path is not None
        assert "stage_000_test_20260707_120000_123456.json" == report_path.name

    def test_report_in_cmd_when_staging(self):
        args = mod._parse_args(_argv("--query", "test", "--stage-to-paper-raw"))
        cmd, _, _, _ = mod._build_command("test", 0, "20260707_120000_123456", args)
        assert "--report" in cmd

    def test_log_path_includes_index_and_batch_stamp(self):
        args = mod._parse_args(_argv("--query", "test"))
        _, _, _, log_path = mod._build_command("test", 0, "20260707_120000_123456", args)
        assert "discover_000_test_20260707_120000_123456.log" == log_path.name

    def test_hide_existing_passed_through(self):
        args = mod._parse_args(_argv("--query", "t", "--hide-existing"))
        cmd, _, _, _ = mod._build_command("t", 0, "bs", args)
        assert "--hide-existing" in cmd

    def test_hide_existing_not_in_cmd_when_absent(self):
        args = mod._parse_args(_argv("--query", "t"))
        cmd, _, _, _ = mod._build_command("t", 0, "bs", args)
        assert "--hide-existing" not in cmd

    def test_duplicate_queries_distinct_output_dir(self):
        args = mod._parse_args(_argv("--query", "dup", "--query", "dup"))
        bs = "20260707_120000_123456"
        _, out0, _, _ = mod._build_command("dup", 0, bs, args)
        _, out1, _, _ = mod._build_command("dup", 1, bs, args)
        assert str(out0) != str(out1)

    def test_duplicate_queries_distinct_log_paths(self):
        args = mod._parse_args(_argv("--query", "dup", "--query", "dup"))
        bs = "20260707_120000_123456"
        _, _, _, log0 = mod._build_command("dup", 0, bs, args)
        _, _, _, log1 = mod._build_command("dup", 1, bs, args)
        assert str(log0) != str(log1)
        assert "000_dup" in str(log0)
        assert "001_dup" in str(log1)

    def test_duplicate_queries_distinct_report_path_staging(self):
        args = mod._parse_args(_argv("--query", "dup", "--query", "dup", "--stage-to-paper-raw"))
        bs = "20260707_120000_123456"
        _, _, rep0, _ = mod._build_command("dup", 0, bs, args)
        _, _, rep1, _ = mod._build_command("dup", 1, bs, args)
        assert rep0 != rep1
        assert "stage_000_dup" in str(rep0)
        assert "stage_001_dup" in str(rep1)


# ---------------------------------------------------------------------------
# main_internal
# ---------------------------------------------------------------------------


class TestMainInternal:
    def _fake_batch(self, statuses: list[str], *, exit_code: int = 0):
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

    def test_batch_stamp_in_all_generated_paths(self, tmp_path: Path):
        """The active path writes one coordinator v3 batch report."""
        report_dir = tmp_path / "reports"
        log_dir = tmp_path / "logs"
        out_dir = tmp_path / "candidates"

        with patch.object(mod, "run_discovery_batch", return_value=self._fake_batch(["success", "success"])):
            argv = [
                "--query", "alpha",
                "--query", "beta",
                "--output-dir", str(out_dir),
                "--log-dir", str(log_dir),
                "--report-dir", str(report_dir),
            ]
            rc = mod.main_internal(argv)

        assert rc == 0
        report_files = list(report_dir.iterdir())
        assert len(report_files) == 1
        report = json.loads(report_files[0].read_text(encoding="utf-8"))
        bs = report["batch_stamp"]
        assert len(bs) > 15  # high-res
        assert bs.count("_") >= 2  # YYYYMMDD_HHMMSS_ffffff
        assert report["schema_version"] == "3.0"
        assert report["batch_report"]["schema_version"] == "3.0"
        assert report["queries_count"] == 2

    def test_nonzero_exit_code_1_on_failure(self, tmp_path: Path):
        """Coordinator exit code is returned and reflected in query rows."""
        report_dir = tmp_path / "reports"
        log_dir = tmp_path / "logs"
        out_dir = tmp_path / "candidates"

        with patch.object(mod, "run_discovery_batch", return_value=self._fake_batch(["success", "failed", "success"], exit_code=1)):
            argv = [
                "--query", "ok",
                "--query", "fail",
                "--query", "ok2",
                "--output-dir", str(out_dir),
                "--log-dir", str(log_dir),
                "--report-dir", str(report_dir),
            ]
            rc = mod.main_internal(argv)

        assert rc == 1
        report_files = list(report_dir.iterdir())
        report = json.loads(report_files[0].read_text(encoding="utf-8"))
        assert report["queries_count"] == 3
        assert [q["query"] for q in report["queries"]] == ["kw0", "kw1", "kw2"]
        assert report["queries"][1]["returncode"] == 1

    def test_main_calls_single_coordinator_with_global_worker_cap(self, tmp_path: Path):
        report_dir = tmp_path / "reports"
        log_dir = tmp_path / "logs"
        out_dir = tmp_path / "candidates"

        with patch.object(mod, "run_discovery_batch", return_value=self._fake_batch(["success", "success"])) as run_batch:
            argv = [
                "--query", "alpha",
                "--query", "beta",
                "--max-workers", "2",
                "--output-dir", str(out_dir),
                "--log-dir", str(log_dir),
                "--report-dir", str(report_dir),
            ]
            rc = mod.main_internal(argv)

        assert rc == 0
        queries, = run_batch.call_args.args
        assert queries == ["alpha", "beta"]
        assert run_batch.call_args.kwargs["max_workers"] == 2
        options = run_batch.call_args.kwargs["options"]
        assert options.output_dir.parent == out_dir
        assert options.notebook_dir == mod.DISCOVERY_KEYWORD_NOTEBOOK_DIR

    def test_main_aggregates_stage_reports(self, tmp_path: Path):
        """stage_summary comes from the in-memory batch aggregate."""
        report_dir = tmp_path / "reports"
        report_dir.mkdir(parents=True)
        log_dir = tmp_path / "logs"
        out_dir = tmp_path / "candidates"

        batch = self._fake_batch(["success", "success"])
        batch.aggregate["candidates"]["emitted"] = 2
        with patch.object(mod, "run_discovery_batch", return_value=batch):
            argv = [
                "--query", "alpha",
                "--query", "beta",
                "--stage-to-paper-raw",
                "--output-dir", str(out_dir),
                "--log-dir", str(log_dir),
                "--report-dir", str(report_dir),
            ]
            rc = mod.main_internal(argv)

        assert rc == 0
        report_files = [f for f in report_dir.iterdir() if f.name.startswith("concurrent_discovery_")]
        assert len(report_files) == 1
        report = json.loads(report_files[0].read_text(encoding="utf-8"))
        ss = report.get("stage_summary")
        assert ss is not None
        assert ss["emitted"] == 2

    def test_keyboard_interrupt_propagates(self, tmp_path: Path):
        """KeyboardInterrupt from coordinator should propagate."""
        report_dir = tmp_path / "reports"
        log_dir = tmp_path / "logs"
        out_dir = tmp_path / "candidates"

        argv = [
            "--query", "boom",
            "--output-dir", str(out_dir),
            "--log-dir", str(log_dir),
            "--report-dir", str(report_dir),
        ]
        with patch.object(mod, "run_discovery_batch", side_effect=KeyboardInterrupt()), pytest.raises(KeyboardInterrupt):
            mod.main_internal(argv)

    def test_log_dir_created_if_not_exists(self, tmp_path: Path):
        """Log directory is auto-created."""
        log_dir = tmp_path / "nonexistent_logs"
        report_dir = tmp_path / "reports"
        out_dir = tmp_path / "candidates"

        argv = [
            "--query", "mkdir-test",
            "--output-dir", str(out_dir),
            "--log-dir", str(log_dir),
            "--report-dir", str(report_dir),
        ]
        with patch.object(mod, "run_discovery_batch", return_value=self._fake_batch(["success"])):
            rc = mod.main_internal(argv)
        assert rc == 0
        assert log_dir.is_dir()

    def test_report_dir_created_if_not_exists(self, tmp_path: Path):
        """Report directory is auto-created."""
        log_dir = tmp_path / "logs"
        report_dir = tmp_path / "nonexistent_reports"
        out_dir = tmp_path / "candidates"

        argv = [
            "--query", "mkdir-test",
            "--output-dir", str(out_dir),
            "--log-dir", str(log_dir),
            "--report-dir", str(report_dir),
        ]
        with patch.object(mod, "run_discovery_batch", return_value=self._fake_batch(["success"])):
            rc = mod.main_internal(argv)
        assert rc == 0
        assert report_dir.is_dir()
