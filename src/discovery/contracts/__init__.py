"""Discovery v4 strict contracts — canonical data models, enums, and parsers.

All dataclasses are frozen with ``__post_init__`` type/value checks and
``from_dict_strict()`` that rejects unknown/missing fields.

Dependency rule: contracts never import from stores, providers, runtime,
execution, reporting, coordinator, or migration modules.
"""

from src.discovery.contracts.enums import (
    DrainOutcome,
    JournalStateV4,
    LaneExecutionState,
    LaneStopReason,
    QueryLanguage,
    ShutdownReason,
)
from src.discovery.contracts.lane_state import (
    LANE_STATE_SCHEMA_VERSION_V4,
    CursorTransactionV4,
    LaneStateV4,
)
from src.discovery.contracts.manifest import (
    WORKSPACE_MANIFEST_SCHEMA_VERSION_V4,
    ActiveGenerationPointerV4,
    DiscoveryWorkspaceManifestV4,
)
from src.discovery.contracts.notebook import (
    NOTEBOOK_SCHEMA_VERSION_V4,
    NOTEBOOK_TOP_LEVEL_FIELDS,
    NOTEBOOK_TOP_LEVEL_REQUIRED,
    NotebookContractError,
    KeywordNotebookV4,
)
from src.discovery.contracts.page_journal import (
    PAGE_SCHEMA_VERSION_V4,
    PAGE_V4_FIELDS,
    ProviderPageJournalV4,
    UnexpectedNonV4StateError,
)
from src.discovery.contracts.report import (
    REPORT_SCHEMA_VERSION_V4,
    BatchDiscoveryReportV4,
    KeywordDiscoveryReportV4,
    LaneReportV4,
)

__all__ = [
    # Enums
    "DrainOutcome",
    "JournalStateV4",
    "LaneExecutionState",
    "LaneStopReason",
    "QueryLanguage",
    "ShutdownReason",
    # Lane state
    "LANE_STATE_SCHEMA_VERSION_V4",
    "CursorTransactionV4",
    "LaneStateV4",
    # Manifest
    "WORKSPACE_MANIFEST_SCHEMA_VERSION_V4",
    "ActiveGenerationPointerV4",
    "DiscoveryWorkspaceManifestV4",
    # Notebook
    "NOTEBOOK_SCHEMA_VERSION_V4",
    "NOTEBOOK_TOP_LEVEL_FIELDS",
    "NOTEBOOK_TOP_LEVEL_REQUIRED",
    "NotebookContractError",
    "KeywordNotebookV4",
    # Page journal
    "PAGE_SCHEMA_VERSION_V4",
    "PAGE_V4_FIELDS",
    "ProviderPageJournalV4",
    "UnexpectedNonV4StateError",
    # Report
    "REPORT_SCHEMA_VERSION_V4",
    "BatchDiscoveryReportV4",
    "KeywordDiscoveryReportV4",
    "LaneReportV4",
]
