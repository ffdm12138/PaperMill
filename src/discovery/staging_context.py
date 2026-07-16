"""Per-drain owner of the authoritative registry snapshot and transaction."""
from __future__ import annotations

from pathlib import Path

from filelock import FileLock
from src.discovery.stage_transaction import DiscoveryStageTransaction, StageTransactionConfigurationError
from src.discovery.workspace_registry import WorkspaceRegistrySnapshot, build_workspace_registry
from src.library.paper_number_ledger import PaperNumberLedger
from src.discovery.staging_metrics import NullStagingMetricsObserver, StagingMetricsObserver


class DiscoveryStagingContext:
    def __init__(self, *, registry_snapshot: WorkspaceRegistrySnapshot,
                 transaction: DiscoveryStageTransaction) -> None:
        self.registry_snapshot = registry_snapshot
        self.transaction = transaction

    @property
    def duplicate_index(self):
        return self.transaction.registry_snapshot.doi_index

    @property
    def workspace_index(self):
        return self.transaction.registry_snapshot.workspace_id_index

    @property
    def registry(self):
        return self.transaction.registry_snapshot

    @classmethod
    def create(cls, *, paper_raw_dir: str | Path, papers_dir: str | Path,
               ledger_path: str | Path, prepare_allocation: bool = True) -> "DiscoveryStagingContext":
        observer = NullStagingMetricsObserver()
        return cls.create_with_observer(
            paper_raw_dir=paper_raw_dir, papers_dir=papers_dir, ledger_path=ledger_path,
            prepare_allocation=prepare_allocation, observer=observer,
        )

    @classmethod
    def create_with_observer(cls, *, paper_raw_dir: str | Path, papers_dir: str | Path,
                             ledger_path: str | Path, prepare_allocation: bool = True,
                             observer: StagingMetricsObserver) -> "DiscoveryStagingContext":
        observer.staging_context_build()
        ledger = PaperNumberLedger(ledger_path)
        raw_root = Path(paper_raw_dir)
        raw_root.mkdir(parents=True, exist_ok=True)
        with FileLock(str(raw_root / ".paper_raw_write.lock")):
            built = build_workspace_registry(
                paper_raw_dir=raw_root, papers_dir=papers_dir, ledger=ledger, observer=observer,
            )
        if not built.complete or built.registry is None:
            raise StageTransactionConfigurationError(
                "WorkspaceRegistry is incomplete: " + ";".join(map(str, built.issues))
            )
        transaction = DiscoveryStageTransaction(
            paper_raw_dir=Path(paper_raw_dir), papers_dir=Path(papers_dir),
            ledger_path=Path(ledger_path), registry_snapshot=built.registry,
            observer=observer,
        )
        return cls(registry_snapshot=built.registry, transaction=transaction)
