#!/usr/bin/env python
"""Synthetic full-transaction benchmark with production instrumentation."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.discovery.staging_context import DiscoveryStagingContext
from src.discovery.staging_metrics import CollectingStagingMetricsObserver
from src.library.paper_number_ledger import PaperNumberLedger
from src.staging.network_metadata_staging import stage_network_metadata_records
from tests.factories.discovery_factory import create_discovery_candidate
from tests.factories.paper_raw_factory import create_network_metadata_workspaces_bulk


def run_benchmark(*, existing_workspaces: int, new_records: int,
                  unsettled_workspaces: int, repeat: int) -> dict:
    runs: list[dict] = []
    for run_number in range(1, repeat + 1):
        with tempfile.TemporaryDirectory(prefix="mineru-discovery-benchmark-") as temp:
            root = Path(temp)
            paper_raw, papers, ledger_path = root / "paper_raw", root / "papers", root / "ledger.json"
            create_network_metadata_workspaces_bulk(
                root, count=existing_workspaces, unsettled=unsettled_workspaces)

            observer = CollectingStagingMetricsObserver()
            started = time.perf_counter()
            context = DiscoveryStagingContext.create_with_observer(
                paper_raw_dir=paper_raw, papers_dir=papers, ledger_path=ledger_path,
                observer=observer, prepare_allocation=True,
            )
            cold_seconds = time.perf_counter() - started

            warmup = create_discovery_candidate(
                doi="10.7000/warmup", candidate_id="warmup", page_id="warmup", keyword_id="benchmark")
            stage_network_metadata_records(
                [warmup], paper_raw_dir=paper_raw, papers_dir=papers, ledger_path=ledger_path,
                apply=True, transaction=context.transaction,
            )
            before_staged = observer.records_staged
            before_allocated = observer.paper_numbers_allocated
            durations: list[float] = []
            for n in range(new_records):
                record = create_discovery_candidate(
                    doi=f"10.8000/new.{n}", candidate_id=f"new-{n}",
                    page_id=f"new-page-{n}", keyword_id="benchmark")
                started = time.perf_counter()
                report = stage_network_metadata_records(
                    [record], paper_raw_dir=paper_raw, papers_dir=papers,
                    ledger_path=ledger_path, apply=True, transaction=context.transaction)
                durations.append(time.perf_counter() - started)
                if report["staged"] != 1:
                    raise RuntimeError(f"warm stage failed: {report}")
            ordered = sorted(durations)
            total = sum(durations)
            p95_index = min(len(ordered) - 1, int(len(ordered) * 0.95)) if ordered else 0
            runs.append({
                "run": run_number, "existing_workspaces": existing_workspaces,
                "new_records": new_records, "cold_registry_build_seconds": round(cold_seconds, 6),
                "warm_stage_p50_ms": round(statistics.median(ordered) * 1000, 3) if ordered else 0,
                "warm_stage_p95_ms": round(ordered[p95_index] * 1000, 3) if ordered else 0,
                "warm_stage_max_ms": round(max(ordered) * 1000, 3) if ordered else 0,
                "records_per_second": round(new_records / total, 3) if total else 0,
                "full_registry_builds": observer.full_registry_builds,
                "incremental_registry_refreshes": observer.incremental_registry_refreshes,
                "registry_pre_refreshes": observer.registry_pre_refreshes,
                "registry_post_refreshes": observer.registry_post_refreshes,
                "registry_direct_publishes": observer.registry_direct_publishes,
                "workspace_records_read": observer.workspace_records_read,
                "unsettled_records_read": observer.unsettled_records_read,
                "ledger_loads": observer.ledger_loads, "ledger_saves": observer.ledger_saves,
                "paper_numbers_allocated": observer.paper_numbers_allocated - before_allocated,
                "records_staged": observer.records_staged - before_staged,
            })
    return {"config": {
        "existing_workspaces": existing_workspaces, "new_records": new_records,
        "unsettled_workspaces": unsettled_workspaces, "repeat": repeat,
    }, "runs": runs}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--existing-workspaces", type=int, required=True)
    parser.add_argument("--new-records", type=int, required=True)
    parser.add_argument("--unsettled-workspaces", type=int, default=0)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--json-report", required=True)
    args = parser.parse_args(argv)
    result = run_benchmark(existing_workspaces=args.existing_workspaces,
                           new_records=args.new_records,
                           unsettled_workspaces=args.unsettled_workspaces, repeat=args.repeat)
    destination = Path(args.json_report)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
