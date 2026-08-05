"""Strict, replayable metadata/PDF match receipts (schema 2.0).

Receipts carry the automatic decision (evidence-tiered policy in
``identity_match``) plus an optional manual confirmation; the final decision
is what the workspace state and freeze eligibility use.  Validation replays
the automatic decision from the SAVED evidence — it never re-extracts from
the PDF, so a frozen closure does not depend on a future PyMuPDF version
reproducing the same text.  Explicit re-audits (rematch plan phase) are the
only path that re-extracts.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from src.discovery.models import normalize_doi
from src.utils.file_fingerprint import compute_sha256
from src.utils.timestamps import now_iso
from src.metadata.pdf_identity import (
    CONFIDENCE_LEVELS,
    PdfIdentityEvidence,
)
from src.metadata.identity_match import (
    DECISION_POLICY_VERSION,
    MATCH_METHODS,
    MATCHED_METHODS,
    RECEIPT_STATUS_TO_METADATA_STATE,
    IdentityDecision,
    decide_identity,
)
from src.utils.atomic_io import atomic_write_json

__all__ = [
    "MATCH_METHODS",
    "MATCHED_METHODS",
    "RECEIPT_STATUS_TO_METADATA_STATE",
    "build_match_receipt",
    "write_match_receipt",
    "validate_metadata_match_receipt",
]

SCHEMA_VERSION = "2.0"

# Statuses a manual confirmation may override (identifier_conflict is a
# hard, non-overridable automatic conclusion).
MANUAL_OVERRIDABLE_STATUSES = {"ambiguous", "unverifiable", "related_version"}


def _automatic_decision_dict(decision: IdentityDecision) -> dict:
    return {
        "match_status": decision.match_status,
        "match_method": decision.match_method,
        "pdf_primary_doi": decision.pdf_primary_doi,
        "relation": decision.relation,
        "decision_evidence": decision.details,
    }


def _expected_status_for(method: str) -> str | None:
    if method in {"doi_exact", "doi_medium_bibliographic", "stable_identifier_exact", "manual_confirmed"}:
        return "matched"
    if method == "version_relation":
        return "related_version"
    return method if method in MATCH_METHODS else None


def _validate_manual(manual: object, *, metadata_sha256: str, pdf_sha256: str) -> list[str]:
    if not isinstance(manual, dict):
        return ["manual confirmation must be an object"]
    errors: list[str] = []
    if not str(manual.get("confirmed_by") or "").strip():
        errors.append("manual confirmation confirmed_by is required")
    if not str(manual.get("reason") or "").strip():
        errors.append("manual confirmation reason is required")
    try:
        parsed = datetime.fromisoformat(
            str(manual.get("confirmed_at") or "").replace("Z", "+00:00")
        )
        if parsed.tzinfo is None:
            errors.append("manual confirmation confirmed_at must include timezone")
    except ValueError:
        errors.append("manual confirmation confirmed_at must be RFC3339")
    if manual.get("metadata_sha256") != metadata_sha256:
        errors.append("manual confirmation metadata hash mismatch")
    if manual.get("pdf_sha256") != pdf_sha256:
        errors.append("manual confirmation PDF hash mismatch")
    return errors


def build_match_receipt(
    folder: Path,
    paper_number: str,
    metadata: dict,
    evidence: PdfIdentityEvidence,
    *,
    requested_doi: str = "",
    provider_records: list[str] | None = None,
    manual: dict | None = None,
    matched_at: str | None = None,
) -> dict:
    """Build a schema-2.0 receipt.

    ``matched_at`` is injectable so migration plans pin one timestamp and
    re-apply byte-identically; production callers default to now.
    """
    metadata_path = folder / f"{paper_number}.metadata.json"
    pdf_path = folder / f"{paper_number}.pdf"
    metadata_sha256 = compute_sha256(metadata_path)
    pdf_sha256 = compute_sha256(pdf_path)
    if not requested_doi:
        # Match the legacy behavior: the requested DOI is the metadata DOI
        # unless a caller (e.g. the migration tool) pins it explicitly.
        requested_doi = str((metadata.get("identifiers") or {}).get("doi") or "")
    decision = decide_identity(metadata, evidence, requested_doi=requested_doi)
    automatic = _automatic_decision_dict(decision)
    final_status = automatic["match_status"]
    final_method = automatic["match_method"]
    manual_errors: list[str] = []
    if manual is not None:
        if automatic["match_status"] == "identifier_conflict":
            manual_errors.append("manual confirmation cannot override identifier conflict")
        else:
            manual_errors = _validate_manual(
                manual, metadata_sha256=metadata_sha256, pdf_sha256=pdf_sha256
            )
            if not manual_errors:
                final_status, final_method = "matched", "manual_confirmed"
    return {
        "schema_version": SCHEMA_VERSION,
        "paper_number": paper_number,
        "metadata_sha256": metadata_sha256,
        "pdf_sha256": pdf_sha256,
        "match_status": final_status,
        "match_method": final_method,
        "requested_doi": normalize_doi(requested_doi),
        "pdf_primary_doi": automatic["pdf_primary_doi"],
        "identity_extractor_version": evidence.identity_extractor_version,
        "decision_policy_version": DECISION_POLICY_VERSION,
        "automatic_decision": automatic,
        "final_decision": {
            "match_status": final_status,
            "match_method": final_method,
        },
        "identity_evidence": evidence.to_dict(),
        "decision_evidence": decision.details,
        "provider_records": list(provider_records or []),
        "manual_confirmation": manual,
        "manual_errors": manual_errors,
        "matched_at": matched_at or now_iso(),
    }


def write_match_receipt(folder: Path, receipt: dict) -> Path:
    path = folder / f"{receipt['paper_number']}.metadata_match.json"
    atomic_write_json(path, receipt, indent=2)
    return path


def validate_metadata_match_receipt(
    receipt: dict,
    *,
    metadata_path: Path,
    pdf_path: Path,
    workspace: Path,
    paper_number: str | None = None,
    asset_prefix: str | None = None,
) -> list[str]:
    """Replay the decision from the SAVED evidence; never re-extract.

    v1 receipts are rejected: they require the pdf_identity migration.
    """
    if receipt.get("schema_version") != SCHEMA_VERSION:
        return [
            f"match receipt schema_version {receipt.get('schema_version')} "
            "requires pdf_identity migration"
        ]
    errors: list[str] = []
    required = {
        "paper_number",
        "metadata_sha256",
        "pdf_sha256",
        "match_status",
        "match_method",
        "automatic_decision",
        "final_decision",
        "identity_evidence",
        "provider_records",
    }
    for key in sorted(required - set(receipt)):
        errors.append(f"match receipt missing {key}")
    paper_number = paper_number or metadata_path.name.removesuffix(".metadata.json")
    if receipt.get("paper_number") != paper_number:
        errors.append("match receipt paper_number mismatch")
    method = receipt.get("match_method")
    if method not in MATCH_METHODS:
        errors.append("unknown match method")
    metadata_sha256 = compute_sha256(metadata_path)
    pdf_sha256 = compute_sha256(pdf_path)
    if receipt.get("metadata_sha256") != metadata_sha256:
        errors.append("match receipt metadata hash mismatch")
    if receipt.get("pdf_sha256") != pdf_sha256:
        errors.append("match receipt PDF hash mismatch")

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        stored_evidence = PdfIdentityEvidence.from_dict(
            receipt.get("identity_evidence") or {}
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return errors + [f"invalid match evidence: {exc}"]
    if stored_evidence.confidence not in CONFIDENCE_LEVELS:
        errors.append("invalid evidence confidence")

    # Replay the automatic decision from the saved evidence only.
    automatic = decide_identity(
        metadata, stored_evidence, requested_doi=str(receipt.get("requested_doi") or "")
    )
    if receipt.get("automatic_decision") != _automatic_decision_dict(automatic):
        errors.append("automatic decision replay mismatch")

    expected_status = _expected_status_for(method)
    if expected_status is None:
        errors.append("match method/status consistency violation")
    final_decision = receipt.get("final_decision") or {}
    if final_decision.get("match_status") != receipt.get("match_status") or final_decision.get(
        "match_method"
    ) != method:
        errors.append("final decision mismatch")
    if method == "manual_confirmed":
        if automatic.match_status == "identifier_conflict":
            errors.append("manual confirmation cannot override identifier conflict")
        errors.extend(
            _validate_manual(
                receipt.get("manual_confirmation"),
                metadata_sha256=metadata_sha256,
                pdf_sha256=pdf_sha256,
            )
        )
        if receipt.get("match_status") != "matched":
            errors.append("manual confirmed receipt must have final status matched")
    else:
        if method != automatic.match_method:
            errors.append(f"match method replay mismatch: expected {automatic.match_method}")
        if receipt.get("match_status") != automatic.match_status:
            errors.append(f"match status replay mismatch: expected {automatic.match_status}")
    if expected_status is not None and receipt.get("match_status") != expected_status:
        errors.append(f"match status replay mismatch: expected {expected_status}")
    if method not in MATCHED_METHODS:
        errors.append("metadata/PDF identity is not matched")

    root = workspace.resolve()
    for relative in receipt.get("provider_records") or []:
        path = Path(str(relative))
        target = (workspace / path).resolve()
        if (
            path.is_absolute()
            or ".." in path.parts
            or root not in (target, *target.parents)
            or not target.is_file()
        ):
            errors.append(f"unsafe or missing provider record: {relative}")
    return errors
