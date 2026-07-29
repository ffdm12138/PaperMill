"""DiscoveryStoreBundleV4 — unified store injection point.

The bundle holds the live V4 stores for a single DiscoveryWorkspace.
The coordinator receives this bundle and never constructs individual
stores from flat paths.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.discovery.stores.notebook_store import NotebookStoreV4
from src.discovery.stores.page_journal_store import PageJournalStoreV4
from src.discovery.workspace import DiscoveryWorkspace


@dataclass(frozen=True)
class DiscoveryStoreBundleV4:
    """Live V4 stores for a single DiscoveryWorkspace.

    Immutable after construction — the coordinator, runtime, executor,
    drain, and report builder all receive the same bundle.
    """

    notebooks: NotebookStoreV4
    pages: PageJournalStoreV4

    @classmethod
    def from_workspace(cls, workspace: DiscoveryWorkspace) -> "DiscoveryStoreBundleV4":
        """Create the store bundle from a DiscoveryWorkspace.

        Both stores share the same workspace root.
        """
        return cls(
            notebooks=NotebookStoreV4(workspace),
            pages=PageJournalStoreV4(workspace),
        )
