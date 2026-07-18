from pathlib import Path

import pytest

from scripts.benchmark_discovery_pipeline import run_benchmark

pytestmark = pytest.mark.performance


def test_200_raw_200_formal_500_candidates_have_batch_bounded_io(tmp_path: Path):
    run = run_benchmark(raw_workspaces=200, formal_workspaces=200,
                        pending_candidates=500, batch_size=16, repeat=1)["runs"][0]
    assert run["allocated"] == 300
    assert run["repair_backlog"] == 40
    assert run["journal_page_count"] == 40
    assert run["staging_context_builds"] == 1
    assert run["registry_full_builds"] == 1
    assert run["formal_publication_view_loads"] == 1
    assert run["journal_full_scans"] == 1
    assert run["journal_pages_read"] <= run["journal_page_count"] + 2
    assert run["repair_backlog_probes"] == 20
    # The two-second fairness cap may split a 16-item logical batch on slower
    # Windows filesystems; it must remain bounded by authoritative work, never
    # by journal pages or repair-backlog cardinality.
    assert run["write_lock_acquisitions"] <= 55
    assert run["ledger_loads"] <= run["write_lock_acquisitions"] + 1
    assert run["workspace_fingerprint_calls"] < 1200
    assert run["workspace_fingerprint_calls"] < 500 * 40
    assert run["candidate_lease_renewals"] == 0
    assert run["page_fsyncs"] < 500 * 3
    assert run["ledger_saves"] == 600
