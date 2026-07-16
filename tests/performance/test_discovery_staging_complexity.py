"""Performance contracts over the real staging transaction."""
from __future__ import annotations

from scripts.benchmark_discovery_staging import run_benchmark


def test_benchmark_uses_one_cold_build_and_real_transactions():
    result = run_benchmark(existing_workspaces=20, unsettled_workspaces=2,
                           new_records=5, repeat=1)
    run = result["runs"][0]
    assert run["full_registry_builds"] == 1
    assert run["records_staged"] == 5
    assert run["paper_numbers_allocated"] == 5
    assert run["registry_pre_refreshes"] == 6  # warmup + five measured records
    assert run["registry_post_refreshes"] == 0
    assert run["registry_direct_publishes"] == 6
    assert run["ledger_loads"] <= 7
    assert run["ledger_saves"] <= 12
    assert run["workspace_records_read"] >= 20
    assert run["records_per_second"] > 0


def test_warm_path_does_not_full_rebuild_per_candidate():
    result = run_benchmark(existing_workspaces=50, unsettled_workspaces=0,
                           new_records=8, repeat=1)
    run = result["runs"][0]
    assert run["full_registry_builds"] == 1
    assert run["records_staged"] == 8
    assert run["registry_post_refreshes"] == 0
