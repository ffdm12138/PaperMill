"""DiscoveryStoreBundleV4 — unified store injection point.

The bundle holds all V4 stores for a single DiscoveryWorkspace.
The coordinator receives this bundle and never constructs individual
stores from flat paths.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.discovery.stores.notebook_store import NotebookStoreV4
from src.discovery.stores.page_journal_store import PageJournalStoreV4
from src.discovery.stores.lane_state_store import LaneStateStoreV4
from src.discovery.stores.journal_index import JournalIndexV4
from src.discovery.stores.report_store import ReportStoreV4
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
    index: JournalIndexV4
    reports: ReportStoreV4

    @classmethod
    def from_workspace(cls, workspace: DiscoveryWorkspace) -> "DiscoveryStoreBundleV4":
        """Create a complete store bundle from a DiscoveryWorkspace.

        All stores share the same workspace root.  JournalIndexV4 is
        rebuilt from scratch by scanning page journals.
        """
        page_store = PageJournalStoreV4(workspace)
        return cls(
            notebooks=NotebookStoreV4(workspace),
            lanes=LaneStateStoreV4(workspace),
            pages=page_store,
            index=JournalIndexV4.build(page_store),
            reports=ReportStoreV4(workspace),
        )
