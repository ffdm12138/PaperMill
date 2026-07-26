"""Profile-aware workspace readiness evaluation.

Different ingest sources require different evidence for ``metadata_staged``.
This module defines the :class:`IngestProfile` discriminator and the single
:func:`evaluate_metadata_staged` entry point that every consumer must use
instead of hardcoding "receipt required" or "PDF required" assumptions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from src.workspace.evidence import EvidenceIssue, WorkspaceEvidence


class IngestProfile(str, Enum):
    """The ingest path that produced a workspace, derived from stage_manifest."""

    MANUAL_PDF = "manual_pdf"
    NETWORK_METADATA = "network_metadata"
    NETWORK_METADATA_PDF_FETCH = "network_metadata_pdf_fetch"


# Mapping from known stage_manifest workflow_path values to profiles.
_WORKFLOW_TO_PROFILE: dict[str, IngestProfile] = {
    "manual_pdf": IngestProfile.MANUAL_PDF,
    "network_metadata": IngestProfile.NETWORK_METADATA,
    "network_metadata_pdf_fetch": IngestProfile.NETWORK_METADATA_PDF_FETCH,
}


@dataclass(frozen=True)
class WorkspaceReadiness:
    """Profile-aware staging readiness for one workspace.

    ``ready`` is True only when EVERY required piece of evidence for the
    resolved ingest profile is present and valid. ``missing`` lists the
    evidence categories that are absent or invalid.
    """

    paper_number: str
    profile: IngestProfile | None
    ready: bool
    missing: tuple[str, ...] = ()
    unsettled: bool = True
    issues: tuple[EvidenceIssue, ...] = ()


def resolve_profile(evidence: WorkspaceEvidence) -> IngestProfile | None:
    """Determine the ingest profile from the workspace's stage manifest workflow_path.

    Returns None when the workflow_path is unrecognized — the caller must treat
    this as ``repair_required``.
    """
    wp = evidence.workflow_path.strip()
    if wp in _WORKFLOW_TO_PROFILE:
        return _WORKFLOW_TO_PROFILE[wp]
    return None


def evaluate_metadata_staged(
    evidence: WorkspaceEvidence,
    *,
    profile: IngestProfile | None = None,
) -> WorkspaceReadiness:
    """Evaluate whether *evidence* satisfies the ``metadata_staged`` contract
    for the given (or auto-detected) ingest profile.

    Args:
        evidence: The pure-fact inspection result.
        profile: Explicit profile override. If None, auto-detected from
            ``evidence.workflow_path``.

    Returns:
        A :class:`WorkspaceReadiness` with ``ready=True`` only when all
        profile-required artifacts are present and valid.
    """
    if profile is None:
        profile = resolve_profile(evidence)

    missing: list[str] = []
    issues = list(evidence.issues)

    # Common requirements for all profiles.
    if not evidence.paper_number:
        missing.append("paper_number")
    if not evidence.marker_valid:
        missing.append("marker")
    if not evidence.metadata_valid:
        missing.append("metadata")
    if not evidence.source_records_valid:
        missing.append("source_record")
    if not evidence.stage_manifest_valid:
        missing.append("stage_manifest")
    if not evidence.import_status_valid:
        missing.append("import_status")
    if not evidence.ledger_folder_matches:
        missing.append("ledger_folder_match")

    # Profile-specific requirements.
    if profile == IngestProfile.MANUAL_PDF:
        if not evidence.pdf_present:
            missing.append("pdf")
        if not evidence.asset_manifest_valid:
            missing.append("asset_manifest")
        # Manual PDF does NOT require a discovery receipt.
    elif profile == IngestProfile.NETWORK_METADATA:
        if not evidence.discovery_receipt_valid:
            missing.append("discovery_receipt")
        # Network metadata does NOT require PDF.
    elif profile == IngestProfile.NETWORK_METADATA_PDF_FETCH:
        if not evidence.discovery_receipt_valid:
            missing.append("discovery_receipt")
        if not evidence.pdf_present:
            missing.append("pdf")
        if not evidence.asset_manifest_valid:
            missing.append("asset_manifest")
    else:
        # Unknown profile — cannot determine readiness.
        missing.append("unknown_profile")
        issues.append(EvidenceIssue("unknown_workflow_path", evidence.workflow_path or "<empty>"))

    ready = len(missing) == 0

    # Unsettled: the workspace needs re-scanning (no stable metadata yet).
    from src.library.paper_number_state import LEDGER_ALLOCATING, LEDGER_RESERVED
    unsettled = (
        evidence.ledger_state in {LEDGER_ALLOCATING, LEDGER_RESERVED}
        or not evidence.metadata_valid
        or not evidence.source_records_valid
    )

    return WorkspaceReadiness(
        paper_number=evidence.paper_number,
        profile=profile,
        ready=ready,
        missing=tuple(missing),
        unsettled=unsettled,
        issues=tuple(issues),
    )
