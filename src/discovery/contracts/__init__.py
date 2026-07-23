"""Discovery v4 strict contracts — canonical data models, enums, and parsers.

All dataclasses are frozen with ``__post_init__`` type/value checks and
``from_dict_strict()`` that rejects unknown/missing fields.

Dependency rule: contracts never import from stores, providers, runtime,
execution, reporting, coordinator, or migration modules.
"""

from src.discovery.contracts.enums import (
    CandidateOrigin,
    DrainOutcome,
    JournalStateV4,
    LaneExecutionState,
    LaneStopReason,
    QueryLanguage,
    ShutdownReason,
)
from src.discovery.contracts.candidate import (
    CANDIDATE_ORIGIN_VALUES,
    CANDIDATE_SEED_SCHEMA_VERSION_V4,
    LegacyCandidateSeedV4,
    PendingCandidateV4,
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
from src.discovery.contracts.page_journal import (
    PAGE_SCHEMA_VERSION_V4,
    PAGE_V4_FIELDS,
    ProviderPageJournalV4,
    UnexpectedNonV4StateError,
)

__all__ = [
    # Enums
    "CandidateOrigin",
    "DrainOutcome",
    "JournalStateV4",
    "LaneExecutionState",
    "LaneStopReason",
    "QueryLanguage",
    "ShutdownReason",
    # Candidate
    "CANDIDATE_ORIGIN_VALUES",
    "CANDIDATE_SEED_SCHEMA_VERSION_V4",
    "LegacyCandidateSeedV4",
    "PendingCandidateV4",
    # Lane state
    "LANE_STATE_SCHEMA_VERSION_V4",
    "CursorTransactionV4",
    "LaneStateV4",
    # Manifest
    "WORKSPACE_MANIFEST_SCHEMA_VERSION_V4",
    "ActiveGenerationPointerV4",
    "DiscoveryWorkspaceManifestV4",
    # Page journal
    "PAGE_SCHEMA_VERSION_V4",
    "PAGE_V4_FIELDS",
    "ProviderPageJournalV4",
    "UnexpectedNonV4StateError",
]
