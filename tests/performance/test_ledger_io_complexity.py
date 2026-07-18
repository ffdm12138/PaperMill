"""Ledger I/O must remain O(candidates), independent of registry size."""
from __future__ import annotations

import pytest

from scripts.benchmark_discovery_staging import run_benchmark

pytestmark = pytest.mark.performance


def test_ledger_io_is_bounded_for_large_warm_registry():
    new_records = 100
    result = run_benchmark(
        existing_workspaces=3000, unsettled_workspaces=20,
        new_records=new_records, repeat=1)
    run = result["runs"][0]

    assert run["full_registry_builds"] == 1
    assert run["ledger_loads"] <= new_records + 2
    assert run["ledger_saves"] <= 2 * new_records + 2
    assert run["registry_post_refreshes"] == 0
    assert run["records_staged"] == new_records
