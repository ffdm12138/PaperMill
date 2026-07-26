"""Metadata staging gateway: stable boundary between discovery and paper_raw.

Discovery emits prepared staging records; the gateway submits them to the
authoritative metadata staging service and returns a typed, aggregated v4
result.  No discovery module outside this file should call
``stage_network_metadata_records`` directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.services.network_metadata_staging import stage_network_metadata_records


@dataclass(frozen=True)
class MetadataStagingBatchResultV4:
    """Typed outcome of one discovery staging batch."""

    planned: int = 0
    staged_new: int = 0
    reused_existing: int = 0
    duplicate_observation: int = 0
    invalid: int = 0
    failed_retryable: int = 0
    failed_terminal: int = 0
    items: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "planned": self.planned,
            "staged_new": self.staged_new,
            "reused_existing": self.reused_existing,
            "duplicate_observation": self.duplicate_observation,
            "invalid": self.invalid,
            "failed_retryable": self.failed_retryable,
            "failed_terminal": self.failed_terminal,
            "items": list(self.items),
        }


class MetadataStagingGateway:
    """Gateway from discovery-prepared records to Metadata v2.0 / paper_raw.

    The gateway owns the call to ``stage_network_metadata_records`` and the
    mapping from its raw item statuses to the v4 result vocabulary.  Discovery
    code above this layer only sees prepared staging records and
    ``MetadataStagingBatchResultV4``.
    """

    def __init__(
        self,
        *,
        paper_raw_dir: Path,
        papers_dir: Path,
        ledger_path: Path,
    ) -> None:
        self.paper_raw_dir = Path(paper_raw_dir)
        self.papers_dir = Path(papers_dir)
        self.ledger_path = Path(ledger_path)

    def stage_batch(
        self,
        records: Sequence[Mapping[str, Any]],
        *,
        apply: bool,
        skip_duplicates: bool,
        transaction: Any,
    ) -> MetadataStagingBatchResultV4:
        """Submit one batch of prepared staging records.

        Args:
            records: Prepared metadata v2.0 input records produced by the
                discovery journal drain.
            apply: When ``True``, allocate raw workspaces and write the ledger;
                when ``False``, run a dry-run validation.
            skip_duplicates: If ``True``, duplicate observations are skipped
                rather than reported as failures.
            transaction: The active ``DiscoveryStageTransaction`` (or compatible
                transaction object) passed from the runtime staging context.

        Returns:
            A typed ``MetadataStagingBatchResultV4`` with per-status counters.

        Raises:
            RuntimeError: If the number of result items does not match the input.
        """
        if not records:
            return MetadataStagingBatchResultV4()

        report = stage_network_metadata_records(
            list(records),
            paper_raw_dir=self.paper_raw_dir,
            papers_dir=self.papers_dir,
            ledger_path=self.ledger_path,
            apply=apply,
            dry_run=not apply,
            skip_duplicates=skip_duplicates,
            transaction=transaction,
        )
        items = list(report.get("items") or [])
        if len(items) != len(records):
            raise RuntimeError(
                f"staging batch result count mismatch: {len(items)} results for "
                f"{len(records)} records"
            )

        counters: dict[str, int] = {
            "planned": 0,
            "staged_new": 0,
            "reused_existing": 0,
            "duplicate_observation": 0,
            "invalid": 0,
            "failed_retryable": 0,
            "failed_terminal": 0,
        }
        for item in items:
            status = str(item.get("status") or "")
            actual_allocated = bool(item.get("actual_allocated"))
            if status == "staged":
                if actual_allocated:
                    counters["staged_new"] += 1
                else:
                    counters["reused_existing"] += 1
            elif status == "planned":
                counters["planned"] += 1
            elif status == "duplicate":
                counters["duplicate_observation"] += 1
            elif status == "invalid":
                counters["invalid"] += 1
            elif status in {"failed_retryable", "repair_required"}:
                counters["failed_retryable"] += 1
            elif status in {"failed_terminal", "failed"}:
                counters["failed_terminal"] += 1
            else:
                # Treat any unknown status as a retryable failure so it surfaces.
                counters["failed_retryable"] += 1

        return MetadataStagingBatchResultV4(
            planned=counters["planned"],
            staged_new=counters["staged_new"],
            reused_existing=counters["reused_existing"],
            duplicate_observation=counters["duplicate_observation"],
            invalid=counters["invalid"],
            failed_retryable=counters["failed_retryable"],
            failed_terminal=counters["failed_terminal"],
            items=tuple(items),
        )


__all__ = [
    "MetadataStagingBatchResultV4",
    "MetadataStagingGateway",
]
