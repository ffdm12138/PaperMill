"""Adapter status compatibility must not blur allocation and reuse facts."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.discovery.stage_transaction import DiscoveryStageResult, StageTransactionError
from src.services.network_metadata_staging import stage_network_metadata_records
from tests.factories.discovery_factory import create_discovery_candidate


class _Transaction:
    def __init__(self, result: DiscoveryStageResult) -> None:
        self.result = result

    def stage_candidate(self, *args, **kwargs) -> DiscoveryStageResult:
        return self.result


class _FairReleaseTransaction:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.next_number = 1

    def stage_candidates_batch(self, candidates, **kwargs):
        self.calls.append(len(candidates))
        results = []
        for index, _candidate in enumerate(candidates):
            if len(self.calls) == 1 and index > 0:
                results.append(DiscoveryStageResult(
                    "failed_retryable",
                    error=StageTransactionError("lock_epoch_budget_exhausted"),
                ))
                continue
            number = f"{self.next_number:016d}"
            self.next_number += 1
            results.append(DiscoveryStageResult(
                "staged", paper_number=number,
                workspace_path=Path("paper_raw") / number,
            ))
        return tuple(results)


@pytest.mark.parametrize(
    ("transaction_status", "external_status", "actual_allocated", "reused_existing"),
    [
        ("staged", "staged", True, False),
        ("reused", "staged", False, True),
        ("duplicate", "duplicate", False, False),
    ],
)
def test_adapter_preserves_allocation_and_reuse_semantics(
    tmp_path: Path, transaction_status: str, external_status: str,
    actual_allocated: bool, reused_existing: bool,
):
    number = "0000000000000001"
    result = DiscoveryStageResult(
        transaction_status, paper_number=number,
        workspace_path=tmp_path / "paper_raw" / number)

    report = stage_network_metadata_records(
        [create_discovery_candidate()], paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers", ledger_path=tmp_path / "ledger.json",
        apply=True, transaction=_Transaction(result))

    item = report["items"][0]
    assert item["status"] == external_status
    assert item["actual_allocated"] is actual_allocated
    assert item["reused_existing"] is reused_existing


def test_fair_lock_release_requeues_unprocessed_candidates(tmp_path: Path):
    transaction = _FairReleaseTransaction()
    records = [
        create_discovery_candidate(
            doi=f"10.1000/fair-{index}", candidate_id=f"candidate-{index}",
            page_id=f"page-{index}",
        )
        for index in range(3)
    ]

    report = stage_network_metadata_records(
        records, paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers", ledger_path=tmp_path / "ledger.json",
        apply=True, transaction=transaction,
    )

    assert transaction.calls == [3, 2]
    assert report["allocated"] == 3
    assert [item["status"] for item in report["items"]] == ["staged"] * 3
