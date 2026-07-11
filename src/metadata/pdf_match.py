"""Strict, replayable metadata/PDF match receipts."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from src.discovery.models import normalize_doi
from src.file_fingerprint import compute_sha256
from src.metadata.normalization import canonical_title
from src.metadata.pdf_identity import (
    CONFIDENCE_LEVELS,
    PdfIdentityEvidence,
    extract_pdf_identity_evidence,
)
from src.utils.atomic_io import atomic_write_json


MATCH_METHODS = {
    "doi_exact",
    "stable_identifier_exact",
    "title_author_year_strict",
    "manual_confirmed",
    "identifier_conflict",
    "mismatch",
}
MATCHED_METHODS = {"doi_exact", "stable_identifier_exact", "title_author_year_strict"}


def _metadata_identity(metadata: dict) -> dict:
    identifiers = metadata.get("identifiers") if isinstance(metadata.get("identifiers"), dict) else {}
    doi = normalize_doi(str(identifiers.get("doi") or ""))
    stable = {
        str(key).casefold(): str(value).strip().casefold()
        for key, value in identifiers.items()
        if str(key).casefold() != "doi" and str(value).strip()
    }
    authors = [a for a in metadata.get("authors") or [] if isinstance(a, dict)]
    families = [str(a.get("family") or a.get("full_name") or "").strip() for a in authors]
    families = [value for value in families if value]
    first = str((metadata.get("first_author") or {}).get("family") or "").strip()
    if not first and families:
        first = families[0]
    return {
        "doi": doi,
        "stable_identifiers": stable,
        "title": canonical_title(str((metadata.get("title") or {}).get("original") or "")),
        "year": int(metadata.get("year")) if str(metadata.get("year") or "").isdigit() else None,
        "first_author": first,
        "author_families": families,
    }


def _evidence_stable_identifiers(evidence: PdfIdentityEvidence) -> dict[str, str]:
    return {kind.casefold(): value.strip().casefold() for kind, value in evidence.extracted_identifiers if value.strip()}


def _automatic_decision(metadata: dict, evidence: PdfIdentityEvidence) -> tuple[str, dict]:
    identity = _metadata_identity(metadata)
    metadata_doi = identity["doi"]
    pdf_dois = set(evidence.extracted_dois)
    metadata_stable = identity["stable_identifiers"]
    pdf_stable = _evidence_stable_identifiers(evidence)
    details = {
        "metadata_doi": metadata_doi,
        "pdf_extracted_dois": sorted(pdf_dois),
        "metadata_stable_identifiers": metadata_stable,
        "pdf_stable_identifiers": pdf_stable,
        "metadata_title": identity["title"],
        "pdf_title": evidence.canonical_title,
        "metadata_year": identity["year"],
        "pdf_year": evidence.publication_year,
        "metadata_first_author": identity["first_author"],
        "pdf_first_author": evidence.first_author_family,
    }

    # Both sides have an explicit DOI: equality is decisive and conflict stops.
    if metadata_doi and pdf_dois:
        return ("doi_exact" if metadata_doi in pdf_dois else "identifier_conflict"), details
    if metadata_doi or pdf_dois:
        return "mismatch", details

    shared_kinds = set(metadata_stable) & set(pdf_stable)
    if metadata_stable and pdf_stable:
        if any(metadata_stable[kind] != pdf_stable[kind] for kind in shared_kinds) or not shared_kinds:
            return "identifier_conflict", details
        return "stable_identifier_exact", details
    if metadata_stable or pdf_stable:
        return "mismatch", details

    metadata_authors = {x.casefold() for x in identity["author_families"]}
    evidence_authors = {x.casefold() for x in evidence.author_families}
    title_ok = bool(evidence.canonical_title) and identity["title"] == evidence.canonical_title
    year_ok = evidence.publication_year is not None and identity["year"] == evidence.publication_year
    first_ok = bool(evidence.first_author_family and identity["first_author"]) and (
        evidence.first_author_family.casefold() == identity["first_author"].casefold()
    )
    overlap = bool(metadata_authors & evidence_authors)
    details.update({"canonical_title_exact": title_ok, "year_match": year_ok, "first_author_match": first_ok, "author_overlap": overlap})
    if title_ok and year_ok and first_ok and overlap and evidence.confidence not in {"heuristic_text", "missing"}:
        return "title_author_year_strict", details
    return "mismatch", details


def _validate_manual(manual: object, *, metadata_sha256: str, pdf_sha256: str) -> list[str]:
    if not isinstance(manual, dict):
        return ["manual confirmation must be an object"]
    errors: list[str] = []
    if not str(manual.get("operator") or "").strip(): errors.append("manual confirmation operator is required")
    if not str(manual.get("reason") or "").strip(): errors.append("manual confirmation reason is required")
    evidence = manual.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append("manual confirmation evidence must be a non-empty array")
    else:
        for index, item in enumerate(evidence):
            if not isinstance(item, dict) or not str(item.get("type") or "").strip() or not str(item.get("detail") or "").strip():
                errors.append(f"manual confirmation evidence[{index}] requires type/detail")
    try:
        parsed = datetime.fromisoformat(str(manual.get("confirmed_at") or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None: errors.append("manual confirmation confirmed_at must include timezone")
    except ValueError:
        errors.append("manual confirmation confirmed_at must be RFC3339")
    if manual.get("metadata_sha256") != metadata_sha256: errors.append("manual confirmation metadata hash mismatch")
    if manual.get("pdf_sha256") != pdf_sha256: errors.append("manual confirmation PDF hash mismatch")
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
) -> dict:
    metadata_path = folder / f"{paper_number}.metadata.json"
    pdf_path = folder / f"{paper_number}.pdf"
    automatic_method, details = _automatic_decision(metadata, evidence)
    metadata_sha256 = compute_sha256(metadata_path)
    pdf_sha256 = compute_sha256(pdf_path)
    method = automatic_method
    manual_errors: list[str] = []
    if manual is not None:
        if automatic_method == "identifier_conflict":
            manual_errors.append("manual confirmation cannot override identifier conflict")
        else:
            manual_errors = _validate_manual(manual, metadata_sha256=metadata_sha256, pdf_sha256=pdf_sha256)
            if not manual_errors:
                method = "manual_confirmed"
            else:
                method = "mismatch"
    status = "matched" if method in MATCHED_METHODS else "manual_confirmed" if method == "manual_confirmed" else "identifier_conflict" if method == "identifier_conflict" else "mismatch"
    return {
        "schema_version": "1.0",
        "paper_number": paper_number,
        "metadata_sha256": metadata_sha256,
        "pdf_sha256": pdf_sha256,
        "match_status": status,
        "match_method": method,
        "requested_doi": normalize_doi(requested_doi),
        "identity_evidence": evidence.to_dict(),
        "decision_evidence": details,
        "provider_records": list(provider_records or []),
        "manual_confirmation": manual,
        "manual_errors": manual_errors,
        "matched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
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
    """Replay the decision from current assets; never trust receipt status alone."""
    errors: list[str] = []
    required = {"paper_number", "metadata_sha256", "pdf_sha256", "match_status", "match_method", "identity_evidence", "provider_records"}
    for key in sorted(required - set(receipt)):
        errors.append(f"match receipt missing {key}")
    paper_number = paper_number or metadata_path.name.removesuffix(".metadata.json")
    prefix = asset_prefix or metadata_path.name.removesuffix(".metadata.json")
    if receipt.get("paper_number") != paper_number: errors.append("match receipt paper_number mismatch")
    if receipt.get("match_method") not in MATCH_METHODS: errors.append("unknown match method")
    metadata_sha256 = compute_sha256(metadata_path)
    pdf_sha256 = compute_sha256(pdf_path)
    if receipt.get("metadata_sha256") != metadata_sha256: errors.append("match receipt metadata hash mismatch")
    if receipt.get("pdf_sha256") != pdf_sha256: errors.append("match receipt PDF hash mismatch")

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        stored_evidence = PdfIdentityEvidence.from_dict(receipt.get("identity_evidence") or {})
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return errors + [f"invalid match evidence: {exc}"]
    if stored_evidence.confidence not in CONFIDENCE_LEVELS: errors.append("invalid evidence confidence")
    markdown = workspace / f"{prefix}.md"
    conversion = workspace / f"{prefix}.conversion.json"
    used_markdown = any(source.startswith("markdown.") for source in stored_evidence.extraction_sources)
    used_conversion = "conversion.manifest" in stored_evidence.extraction_sources
    current_evidence = extract_pdf_identity_evidence(
        pdf_path=pdf_path,
        markdown_path=markdown if used_markdown and markdown.exists() else None,
        conversion_manifest_path=conversion if used_conversion and conversion.exists() else None,
    )
    if stored_evidence.to_dict() != current_evidence.to_dict(): errors.append("match identity evidence no longer reproduces from current assets")
    automatic_method, _ = _automatic_decision(metadata, current_evidence)
    claimed_method = receipt.get("match_method")
    expected_status = "mismatch"
    if claimed_method == "manual_confirmed":
        if automatic_method == "identifier_conflict": errors.append("manual confirmation cannot override identifier conflict")
        errors.extend(_validate_manual(receipt.get("manual_confirmation"), metadata_sha256=metadata_sha256, pdf_sha256=pdf_sha256))
        expected_status = "manual_confirmed"
    else:
        if claimed_method != automatic_method: errors.append(f"match method replay mismatch: expected {automatic_method}")
        expected_status = "matched" if automatic_method in MATCHED_METHODS else automatic_method if automatic_method == "identifier_conflict" else "mismatch"
    if receipt.get("match_status") != expected_status: errors.append(f"match status replay mismatch: expected {expected_status}")
    if claimed_method not in MATCHED_METHODS | {"manual_confirmed"}: errors.append("metadata/PDF identity is not matched")

    root = workspace.resolve()
    for relative in receipt.get("provider_records") or []:
        path = Path(str(relative))
        target = (workspace / path).resolve()
        if path.is_absolute() or ".." in path.parts or root not in (target, *target.parents) or not target.is_file():
            errors.append(f"unsafe or missing provider record: {relative}")
    return errors
