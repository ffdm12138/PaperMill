"""DiscoveryStoreBundleV4 — unified store injection point.

The bundle holds all V4 stores for a single DiscoveryWorkspace.
The coordinator receives this bundle and never constructs individual
stores from flat paths.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.discovery.stores.notebook_store import NotebookStoreV4
from src.discovery.stores.page_journal_store import PageJournalStoreV4
from src.discovery.stores.lane_state_store import LaneStateStoreV4
from src.discovery.stores.pending_candidate_store import PendingCandidateStoreV4
from src.discovery.stores.journal_index import JournalIndexV4
from src.discovery.stores.report_store import ReportStoreV4
from src.discovery.stores.migration_receipt_store import MigrationReceiptStoreV4
from src.discovery.workspace import DiscoveryWorkspace


@dataclass(frozen=True)
class DiscoveryStoreBundleV4:
    """All V4 stores for a single DiscoveryWorkspace.

    Immutable after construction — the coordinator, runtime, executor,
    drain, and report builder all receive the same bundle.
    """

    notebooks: NotebookStoreV4
    lanes: LaneStateStoreV4
    pages: PageJournalStoreV4
    pending: PendingCandidateStoreV4
    index: JournalIndexV4
    reports: ReportStoreV4
    receipts: MigrationReceiptStoreV4 | None = None

    @classmethod
    def from_workspace(
        cls,
        workspace: DiscoveryWorkspace,
        *,
        migration_receipts_dir: Path | None = None,
    ) -> "DiscoveryStoreBundleV4":
        """Create a complete store bundle from a DiscoveryWorkspace.

        All stores share the same workspace root.  JournalIndexV4 is
        rebuilt from scratch by scanning page journals.  ``receipts`` stays
        ``None`` for normal production batches; migration-driven drains pass
        an explicit ``migration_receipts_dir`` (outside the manifest-hashed
        generation tree) so consumed legacy seeds leave a durable receipt.
        """
        page_store = PageJournalStoreV4(workspace)
        return cls(
            notebooks=NotebookStoreV4(workspace),
            lanes=LaneStateStoreV4(workspace),
            pages=page_store,
            pending=PendingCandidateStoreV4(workspace),
            index=JournalIndexV4.build(page_store),
            reports=ReportStoreV4(workspace),
            receipts=(
                MigrationReceiptStoreV4(receipts_dir=migration_receipts_dir)
                if migration_receipts_dir is not None
                else None
            ),
        )
