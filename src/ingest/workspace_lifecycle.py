"""Unified workspace lifecycle inspection — backward-compatibility layer.

Delegates fact-gathering to :mod:`workspace_evidence` and readiness evaluation
to :mod:`workspace_readiness`. New code should import those modules directly.
This module exists so existing callers (ledger recovery, formalize gate,
duplicate index, and ledger recovery) continue to work without modification.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.ingest.workspace_evidence import (
    EvidenceIssue,
    WorkspaceEvidence,
    inspect_workspace_evidence,
)
from src.ingest.workspace_readiness import (
    IngestProfile,
    WorkspaceReadiness,
    evaluate_metadata_staged,
    resolve_profile,
)


@dataclass(frozen=True)
class WorkspaceLifecycleInspection:
    """Complete snapshot of one workspace's lifecycle state.

    This is a backward-compatibility wrapper that combines
    :class:`~src.ingest.workspace_evidence.WorkspaceEvidence` (pure file facts)
    and :class:`~src.ingest.workspace_readiness.WorkspaceReadiness`
    (profile-aware readiness). New code should use those types directly.

    Attributes:
        complete_for_metadata_staged: DEPRECATED. Delegates to profile-aware
            readiness evaluation. For network_metadata profile this requires a
            discovery receipt; for manual_pdf profile it does not.
    """

    paper_number: str
    ledger_state: str | None
    marker_valid: bool
    metadata_valid: bool
    source_record_valid: bool
    receipt_valid: bool
    stage_manifest_valid: bool
    import_status_valid: bool
    discovery_identity_valid: bool
    complete_for_metadata_staged: bool
    unsettled: bool
    repair_required: bool
    errors: tuple[str, ...] = ()
    metadata_doi: str = ""

    # Extended fields from the new evidence/readiness modules.
    evidence: WorkspaceEvidence | None = None
    readiness: WorkspaceReadiness | None = None
    ingest_profile: IngestProfile | None = None


def inspect_workspace_lifecycle(
    workspace: Path,
    *,
    ledger_item: dict[str, Any] | None = None,
    expected_discovery_identity: dict[str, str] | None = None,
) -> WorkspaceLifecycleInspection:
    """Inspect one workspace and return a structured lifecycle snapshot.

    Delegates fact-gathering to :func:`inspect_workspace_evidence` and
    readiness evaluation to :func:`evaluate_metadata_staged`. The
    ``expected_discovery_identity`` check is applied on top of the
    evidence (for consumers that need identity-match validation).

    Args:
        workspace: Path to the workspace directory.
        ledger_item: The ledger ``items[number]`` dict, if available.
        expected_discovery_identity: If set, the receipt identity must match
            these fields (candidate_id, page_id, keyword_id, provider,
            normalized_doi) for ``discovery_identity_valid`` to be True.

    Returns:
        A fully-populated :class:`WorkspaceLifecycleInspection`.
    """
    import json
    from src.discovery.models import normalize_doi

    # Gather pure facts.
    evidence = inspect_workspace_evidence(workspace, ledger_item=ledger_item)

    # Profile-aware readiness evaluation.
    readiness = evaluate_metadata_staged(evidence)

    # Re-evaluate with expected_discovery_identity if provided.
    discovery_identity_valid = evidence.discovery_receipt_valid
    extra_errors: list[str] = []
    if expected_discovery_identity and evidence.discovery_receipt_valid and evidence.discovery_identity:
        for key, expected_val in expected_discovery_identity.items():
            actual = evidence.discovery_identity.get(key, "")
            if normalize_doi(str(actual)) == normalize_doi(str(expected_val)):
                continue
            if str(actual) != str(expected_val):
                discovery_identity_valid = False
                extra_errors.append(f"receipt_identity_mismatch:{key}")
                break

    # Flatten for backward compatibility.
    complete = readiness.ready
    if expected_discovery_identity and not discovery_identity_valid:
        complete = False

    all_errors = tuple(
        str(i) for i in evidence.issues
    ) + tuple(extra_errors)

    return WorkspaceLifecycleInspection(
        paper_number=evidence.paper_number,
        ledger_state=evidence.ledger_state,
        marker_valid=evidence.marker_valid,
        metadata_valid=evidence.metadata_valid,
        source_record_valid=evidence.source_records_valid,
        receipt_valid=evidence.discovery_receipt_valid,
        stage_manifest_valid=evidence.stage_manifest_valid,
        import_status_valid=evidence.import_status_valid,
        discovery_identity_valid=discovery_identity_valid,
        complete_for_metadata_staged=complete,
        unsettled=readiness.unsettled,
        repair_required=len(all_errors) > 0,
        errors=all_errors,
        metadata_doi=evidence.normalized_doi,
        evidence=evidence,
        readiness=readiness,
        ingest_profile=readiness.profile,
    )
