from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from tests.helpers.relevance_profiles import bind_test_relevance_profile

import scripts.discover_papers_concurrent as mod
from src.discovery.keyword_notebook import KeywordNotebookStore, keyword_id


pytestmark = pytest.mark.unit


def _seed_ready(root: Path, keyword_zh: str) -> None:
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


def _fake_batch(keywords: list[str], statuses: list[str] | None = None, *, exit_code: int = 0):
    statuses = statuses or ["success"] * len(keywords)
    reports = [
        SimpleNamespace(keyword_zh=keyword, status=status)
        for keyword, status in zip(keywords, statuses, strict=True)
    ]
    aggregate = {
        "keywords": {
            "total": len(statuses),
            "success": statuses.count("success"),
            "partial_success": statuses.count("partial_success"),
            "failed": statuses.count("failed"),
            "skipped": statuses.count("skipped"),
        },
        "refresh": {"pages_requested": 0, "pages_recovered": 0, "provider_failures": 0},
        "backfill": {"pages_requested": 0, "pages_recovered": 0, "states_exhausted": 0, "provider_failures": 0},
        "candidates": {"staged": 0, "emitted": 0, "existing_duplicates": 0, "duplicate_observations": 0},
    }

    class _Batch:
        status = "success" if exit_code == 0 else "failed"

        def __init__(self):
            self.keywords = reports
            self.aggregate = aggregate
            self.exit_code = exit_code

        def to_dict(self):
            return {
                "schema_version": "3.0",
                "status": self.status,
                "exit_code": self.exit_code,
                "keywords": [
                    {"keyword_zh": row.keyword_zh, "status": row.status}
                    for row in self.keywords
                ],
                "aggregate": self.aggregate,
            }

    return _Batch()


def test_parse_args_reads_chinese_keyword_file(tmp_path: Path):
    path = tmp_path / "keywords.txt"
    path.write_text("风吹雪\n# comment\n雪粒破碎\n风洞实验\n", encoding="utf-8")
    args = mod._parse_args(["--keywords-file", str(path)])
    assert args.keywords == ["风吹雪", "雪粒破碎", "风洞实验"]
    assert args.max_workers == 4


def test_parse_args_rejects_free_queries_and_out_of_range_batch():
    with pytest.raises(SystemExit) as exc:
        mod._parse_args(["--query", "snow"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        mod._parse_args(["--keyword-zh", "风吹雪"])
    assert exc.value.code == 2


def test_parse_args_enforces_three_or_four_workers():
    argv = sum((["--keyword-zh", value] for value in ["甲类", "乙类", "丙类"]), [])
    with pytest.raises(SystemExit) as exc:
        mod._parse_args([*argv, "--max-workers", "2"])
    assert exc.value.code == 2


def test_until_exhausted_decoupled_from_max_pages_total():
    """--until-exhausted no longer requires --max-pages-total specifically;
    it accepts --max-provider-requests-total as the safety valve, but still
    requires AT LEAST one valve (no giant-integer unbounded runs)."""
    argv = sum((["--keyword-zh", value] for value in ["甲类", "乙类", "丙类"]), [])
    # No valve at all -> rejected.
    with pytest.raises(SystemExit) as exc:
        mod._parse_args([*argv, "--mode", "backfill", "--until-exhausted"])
    assert exc.value.code == 2
    # provider-request valve only -> accepted (decoupled from max-pages-total).
    args = mod._parse_args([*argv, "--mode", "backfill", "--until-exhausted",
                            "--max-provider-requests-total", "50"])
    assert args.until_exhausted is True
    assert args.max_pages_total is None
    assert args.max_provider_requests_total == 50
    # page valve only -> still accepted.
    args = mod._parse_args([*argv, "--mode", "backfill", "--until-exhausted",
                            "--max-pages-total", "10"])
    assert args.max_pages_total == 10


def test_dry_run_is_read_only_and_lists_all_bilingual_provider_lanes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    notebooks = tmp_path / "notebooks"
    keywords = ["风吹雪", "雪粒破碎", "风洞实验"]
    for keyword in keywords:
        _seed_ready(notebooks, keyword)
    report_dir = tmp_path / "reports"
    output_dir = tmp_path / "output"

    argv = sum((["--keyword-zh", value] for value in keywords), [])
    with patch.object(mod, "run_discovery_batch") as run_batch:
        rc = mod.main_internal([
            *argv,
            "--dry-run",
            "--keyword-notebook-dir", str(notebooks),
            "--report-dir", str(report_dir),
            "--output-dir", str(output_dir),
        ])

    assert rc == 0
    run_batch.assert_not_called()
    assert not report_dir.exists()
    assert not output_dir.exists()
    payload = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    assert [row["keyword_zh"] for row in payload["keywords"]] == keywords
    assert all(len(row["provider_lanes"]) == 8 for row in payload["keywords"])
    assert {q["language"] for q in payload["keywords"][0]["queries"]} == {"zh", "en"}
    plan = payload["keywords"][0]
    assert plan["refresh_pages"] == 2
    assert plan["backfill_pages"] == 5
    assert plan["worker_count"] == 4
    assert plan["page_budget"] == {
        "max_pages_total": None,
        "max_provider_requests_total": None,
        "refresh_pages_per_lane": 2,
        "backfill_pages_per_lane": 5,
    }
    lane = plan["provider_lanes"][0]
    assert lane["generation"] == 1
    assert lane["request_signature"] == ""
    assert lane["exhausted"] is False
    assert lane["refresh_pages"] == 2
    assert lane["backfill_pages"] == 5
    assert lane["worker_count"] == 4
    assert lane["page_budget"] == plan["page_budget"]


def test_from_enabled_notebooks_excludes_disabled(tmp_path: Path):
    notebooks = tmp_path / "notebooks"
    for keyword in ["风吹雪", "雪粒破碎", "风洞实验", "积雪输运"]:
        _seed_ready(notebooks, keyword)
    KeywordNotebookStore(notebooks).set_enabled("积雪输运", False)
    args = mod._parse_args([
        "--from-enabled-notebooks",
        "--keyword-notebook-dir", str(notebooks),
    ])
    assert set(args.keywords) == {"风吹雪", "雪粒破碎", "风洞实验"}


def test_main_calls_one_coordinator_with_chinese_notebook_identities(tmp_path: Path):
    notebooks = tmp_path / "notebooks"
    keywords = ["风吹雪", "雪粒破碎", "风洞实验"]
    for keyword in keywords:
        _seed_ready(notebooks, keyword)
    argv = sum((["--keyword-zh", value] for value in keywords), [])
    batch = _fake_batch(keywords)

    with patch.object(mod, "run_discovery_batch", return_value=batch) as run_batch:
        rc = mod.main_internal([
            *argv,
            "--max-workers", "3",
            "--keyword-notebook-dir", str(notebooks),
            "--output-dir", str(tmp_path / "out"),
            "--report-dir", str(tmp_path / "reports"),
        ])

    assert rc == 0
    selected, = run_batch.call_args.args
    assert selected == keywords
    assert run_batch.call_args.kwargs["max_workers"] == 3
    report = json.loads(next((tmp_path / "reports").glob("*.json")).read_text(encoding="utf-8"))
    assert report["keywords_count"] == 3
    assert "queries_count" not in report
    assert all("keyword_zh" in row for row in report["keywords"])


def test_missing_notebook_fails_before_coordinator_and_before_writes(tmp_path: Path):
    keywords = ["风吹雪", "雪粒破碎", "风洞实验"]
    argv = sum((["--keyword-zh", value] for value in keywords), [])
    report_dir = tmp_path / "reports"
    with patch.object(mod, "run_discovery_batch") as run_batch:
        rc = mod.main_internal([
            *argv,
            "--keyword-notebook-dir", str(tmp_path / "missing"),
            "--report-dir", str(report_dir),
        ])
    assert rc == 1
    run_batch.assert_not_called()
    assert not report_dir.exists()
