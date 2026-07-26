"""Legacy discovery contracts used only by the v4 migration."""
from src.migrations.discovery_v4.legacy_contracts.candidate import (
    CANDIDATE_SEED_SCHEMA_VERSION_V4,
    LegacyCandidateSeedV4,
)
from src.migrations.discovery_v4.legacy_contracts.notebook_v3 import (
    LEGACY_NOTEBOOK_SCHEMA_V3,
    LegacyNotebookContractError,
    LegacyNotebookV3,
    convert_notebook_v3_to_v4,
)
from src.migrations.discovery_v4.legacy_contracts.page_journal_v3 import (
    LEGACY_CANDIDATE_STATUS_VALUES,
    LEGACY_PAGE_JOURNAL_SCHEMA_VERSIONS,
    LEGACY_STATUS_MIGRATION_MATRIX,
    LegacyCandidateV3,
    LegacyPageJournalContractError,
    LegacyPageJournalV3,
    classify_legacy_candidate,
    iter_legacy_page_journals,
)

__all__ = [
    "CANDIDATE_SEED_SCHEMA_VERSION_V4",
    "LEGACY_CANDIDATE_STATUS_VALUES",
    "LEGACY_NOTEBOOK_SCHEMA_V3",
    "LEGACY_PAGE_JOURNAL_SCHEMA_VERSIONS",
    "LEGACY_STATUS_MIGRATION_MATRIX",
    "LegacyCandidateSeedV4",
    "LegacyCandidateV3",
    "LegacyNotebookContractError",
    "LegacyNotebookV3",
    "LegacyPageJournalContractError",
    "LegacyPageJournalV3",
    "classify_legacy_candidate",
    "convert_notebook_v3_to_v4",
    "iter_legacy_page_journals",
]
