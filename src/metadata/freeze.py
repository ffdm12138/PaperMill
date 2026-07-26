"""Metadata freeze receipts and strict immutable-closure verification."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path

from src.utils.file_fingerprint import compute_sha256
from src.metadata.citation_readiness import validate_citation_ready
from src.metadata.pdf_match import validate_metadata_match_receipt
from src.metadata.source_records import validate_metadata_source_record_exists
from src.metadata.schema import validate_metadata_schema
from src.utils.atomic_io import atomic_write_json


FREEZE_SCHEMA_VERSION = "1.0"


def _canonical_csl_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _citation_hashes(metadata: dict) -> tuple[dict, list[str]]:
    first = validate_citation_ready(metadata)
    second = validate_citation_ready(metadata)
    errors = list(first.errors)
    if not first.ready or first.generated_csl is None or not first.generated_bibtex:
        errors.append("metadata is not citation-ready")
        return {}, errors
    first_csl = _canonical_csl_bytes(first.generated_csl)
    second_csl = _canonical_csl_bytes(second.generated_csl)
    first_bib = first.generated_bibtex.encode("utf-8")
    second_bib = (second.generated_bibtex or "").encode("utf-8")
    if first_csl != second_csl or first_bib != second_bib:
        errors.append("citation artifacts are not deterministic")
    if b"None" in first_csl or b"None" in first_bib:
        errors.append("citation artifacts contain None placeholders")
    return {
        "csl_sha256": hashlib.sha256(first_csl).hexdigest(),
        "bibtex_sha256": hashlib.sha256(first_bib).hexdigest(),
    }, errors


def _source_record_hashes(folder: Path, metadata: dict) -> tuple[dict[str, str], list[str]]:
    source = metadata.get("source") if isinstance(metadata.get("source"), dict) else {}
    raw_record = str(source.get("raw_record_path") or "")
    errors = validate_metadata_source_record_exists(folder, raw_record, require_nonempty=True)
    hashes: dict[str, str] = {}
    if raw_record and not errors:
        target = folder / raw_record
        hashes[Path(raw_record).as_posix()] = compute_sha256(target)
    # Preserve all persisted provider records that were part of normalization.
    source_dir = folder / "source_records"
    if source_dir.is_dir():
        for path in sorted(p for p in source_dir.rglob("*") if p.is_file()):
            hashes[path.relative_to(folder).as_posix()] = compute_sha256(path)
    if not hashes:
        errors.append("metadata freeze requires persisted provider provenance")
    return hashes, errors


def _load_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _closure(
    folder: Path,
    paper_number: str,
    *,
    asset_prefix: str | None = None,
) -> tuple[dict, dict, dict[str, str], dict, list[str]]:
    prefix = asset_prefix or paper_number
    metadata_path = folder / f"{prefix}.metadata.json"
    pdf_path = folder / f"{prefix}.pdf"
    match_path = folder / f"{prefix}.metadata_match.json"
    errors: list[str] = []
    if len(paper_number) != 16 or not paper_number.isdigit():
        errors.append("metadata freeze requires a 16-digit paper_number")
    if asset_prefix is None and folder.name != paper_number:
        errors.append("raw metadata freeze requires a matching numeric workspace")
    for path, label in ((metadata_path, "metadata"), (pdf_path, "PDF"), (match_path, "match receipt")):
        if not path.is_file(): errors.append(f"missing {label}: {path.name}")
    if errors:
        return {}, {}, {}, {}, errors
    metadata = _load_json(metadata_path, "metadata")
    if metadata.get("paper_number") != paper_number:
        errors.append("metadata.paper_number mismatch")
    errors.extend(validate_metadata_schema(metadata))
    if not str((metadata.get("first_author") or {}).get("family") or "").strip():
        errors.append("metadata.first_author.family is required before freeze")
    if not str(metadata.get("year") or "").isdigit():
        errors.append("metadata.year is required before freeze")
    citation_hashes, citation_errors = _citation_hashes(metadata)
    errors.extend(citation_errors)
    match = _load_json(match_path, "match receipt")
    errors.extend(
        validate_metadata_match_receipt(
            match,
            metadata_path=metadata_path,
            pdf_path=pdf_path,
            workspace=folder,
            paper_number=paper_number,
            asset_prefix=prefix,
        )
    )
    source_hashes, source_errors = _source_record_hashes(folder, metadata)
    errors.extend(source_errors)
    hashes = {
        "metadata_sha256": compute_sha256(metadata_path),
        "pdf_sha256": compute_sha256(pdf_path),
        "metadata_match_sha256": compute_sha256(match_path),
    }
    return hashes, citation_hashes, source_hashes, metadata, errors


def freeze_metadata(folder: Path, paper_number: str) -> dict:
    hashes, citation_hashes, source_hashes, metadata, errors = _closure(folder, paper_number)
    if errors:
        raise ValueError("; ".join(dict.fromkeys(errors)))
    previous = folder / f"{paper_number}.metadata_freeze.json"
    revision = 1
    if previous.exists():
        old = _load_json(previous, "metadata freeze receipt")
        revision = int(old.get("revision") or 0) + 1
    receipt = {
        "schema_version": FREEZE_SCHEMA_VERSION,
        "paper_number": paper_number,
        "metadata_schema_version": str(metadata.get("schema_version") or ""),
        **hashes,
        "citation_ready": True,
        "citation_artifacts": citation_hashes,
        "source_record_hashes": source_hashes,
        "revision": revision,
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    atomic_write_json(previous, receipt, indent=2)
    return receipt


def assert_metadata_frozen(
    folder: Path,
    paper_number: str,
    *,
    asset_prefix: str | None = None,
) -> dict:
    prefix = asset_prefix or paper_number
    freeze_path = folder / f"{prefix}.metadata_freeze.json"
    receipt = _load_json(freeze_path, "metadata freeze receipt")
    errors: list[str] = []
    if receipt.get("schema_version") != FREEZE_SCHEMA_VERSION: errors.append("metadata freeze schema mismatch")
    if receipt.get("paper_number") != paper_number: errors.append("metadata freeze paper_number mismatch")
    hashes, citation_hashes, source_hashes, metadata, closure_errors = _closure(
        folder, paper_number, asset_prefix=asset_prefix
    )
    errors.extend(closure_errors)
    for key, value in hashes.items():
        if receipt.get(key) != value: errors.append(f"frozen {key} mismatch")
    if receipt.get("citation_artifacts") != citation_hashes: errors.append("frozen citation artifact hashes mismatch")
    if receipt.get("source_record_hashes") != source_hashes: errors.append("frozen source record hashes mismatch")
    if receipt.get("metadata_schema_version") != str(metadata.get("schema_version") or ""):
        errors.append("frozen metadata schema version mismatch")
    if errors:
        raise ValueError("; ".join(dict.fromkeys(errors)))
    return receipt


def assert_metadata_content_frozen(folder: Path, paper_number: str) -> dict:
    receipt = _load_json(folder / f"{paper_number}.metadata_freeze.json", "metadata freeze receipt")
    current = compute_sha256(folder / f"{paper_number}.metadata.json")
    if receipt.get("metadata_sha256") != current:
        raise ValueError("frozen metadata hash mismatch")
    return receipt


def assert_metadata_evidence_frozen(folder: Path, paper_number: str) -> dict:
    return assert_metadata_frozen(folder, paper_number)


def assert_metadata_write_allowed(folder: Path, paper_number: str) -> None:
    freeze = folder / f"{paper_number}.metadata_freeze.json"
    if freeze.exists():
        raise PermissionError(f"metadata is frozen: {freeze}")
