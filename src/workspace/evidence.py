"""Single-pass, pure-fact inspection of one ingest workspace."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.workspace.receipt import normalize_receipt_identity
from src.utils.identifiers import normalize_doi
from src.metadata.schema import metadata_doi, validate_metadata_schema
from src.utils.identifiers import PAPER_NUMBER_RE


@dataclass(frozen=True)
class EvidenceIssue:
    category: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.category}:{self.detail}" if self.detail else self.category


@dataclass(frozen=True)
class ParsedDiscoveryIdentity:
    provider: str
    keyword_id: str
    page_id: str
    candidate_id: str
    normalized_doi: str


@dataclass(frozen=True)
class ParsedSourceRecord:
    path: Path
    provider: str
    source_kind: str
    discovery_identities: tuple[ParsedDiscoveryIdentity, ...]


@dataclass(frozen=True)
class ParsedDiscoveryReceipt:
    path: Path
    paper_number: str
    identity: ParsedDiscoveryIdentity


@dataclass(frozen=True)
class WorkspaceEvidence:
    paper_number: str
    folder: Path
    marker_present: bool
    marker_valid: bool
    metadata_present: bool
    metadata_valid: bool
    source_records_present: bool
    source_records_valid: bool
    discovery_receipt_present: bool
    discovery_receipt_valid: bool
    pdf_present: bool
    asset_manifest_present: bool
    asset_manifest_valid: bool
    stage_manifest_present: bool
    stage_manifest_valid: bool
    import_status_present: bool
    import_status_valid: bool
    ledger_folder_matches: bool
    ledger_state: str | None = None
    marker_schema_version: str = ""
    marker_state: str = ""
    marker_folder_name: str = ""
    marker_planned_paper_name: str = ""
    metadata: dict[str, Any] | None = None
    metadata_path: Path | None = None
    formal_catalog_present: bool = False
    formal_catalog: dict[str, Any] | None = None
    formal_asset_manifest_present: bool = False
    formal_asset_manifest: dict[str, Any] | None = None
    normalized_doi: str = ""
    source_kind: str = ""
    source_provider: str = ""
    source_records: tuple[ParsedSourceRecord, ...] = ()
    discovery_receipts: tuple[ParsedDiscoveryReceipt, ...] = ()
    discovery_identity: dict[str, str] | None = None
    workflow_path: str = ""
    issues: tuple[EvidenceIssue, ...] = ()


def _identity_from_context(context: Any) -> ParsedDiscoveryIdentity | None:
    if not isinstance(context, dict):
        return None
    candidate_id = str(context.get("candidate_id") or "").strip()
    page_id = str(context.get("page_id") or "").strip()
    doi = normalize_doi(context.get("normalized_doi") or context.get("doi") or "")
    if not candidate_id or not page_id or not doi:
        return None
    return ParsedDiscoveryIdentity(
        provider=str(context.get("provider") or "").strip().lower(),
        keyword_id=str(context.get("keyword_id") or "").strip(),
        page_id=page_id,
        candidate_id=candidate_id,
        normalized_doi=doi,
    )


def inspect_workspace_evidence(
    workspace: Path, *, ledger_item: dict[str, Any] | None = None,
    expected_paper_number: str = "",
) -> WorkspaceEvidence:
    """Read every JSON artifact at most once and return facts, never policy."""
    folder = Path(workspace)
    issues: list[EvidenceIssue] = []
    cache: dict[Path, tuple[bool, dict[str, Any] | None]] = {}

    def read(path: Path) -> tuple[bool, dict[str, Any] | None]:
        if path in cache:
            return cache[path]
        if not path.is_file():
            result = (False, None)
        else:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                result = (True, value if isinstance(value, dict) else None)
            except (OSError, UnicodeError, json.JSONDecodeError):
                result = (True, None)
        cache[path] = result
        return result

    marker_paths = sorted(folder.glob("*.paper.number"))
    marker_data = [(path, read(path)[1]) for path in marker_paths]
    marker_numbers = [str(data.get("paper_number") or "") for _, data in marker_data if data]
    marker_number = next((n for n in marker_numbers if PAPER_NUMBER_RE.fullmatch(n)), "")
    marker_payload = marker_data[0][1] if len(marker_data) == 1 else None

    metadata_paths = sorted(
        path for path in folder.glob("*.metadata.json")
        if not any(token in path.name for token in (".candidates.json", ".patch.json", ".resolve_report.json"))
    )
    metadata_path = metadata_paths[0] if metadata_paths else None
    metadata = read(metadata_path)[1] if metadata_path else None
    metadata_number = str((metadata or {}).get("paper_number") or "")
    metadata_raw_id = str((metadata or {}).get("paper_raw_id") or "")
    expected_number = (
        str(expected_paper_number)
        if PAPER_NUMBER_RE.fullmatch(str(expected_paper_number or "")) else ""
    )
    inferred_metadata_number = metadata_number or metadata_raw_id
    paper_number = expected_number or marker_number or (
        inferred_metadata_number
        if PAPER_NUMBER_RE.fullmatch(inferred_metadata_number) else ""
    )
    if not paper_number and PAPER_NUMBER_RE.fullmatch(folder.name):
        paper_number = folder.name

    marker_present = bool(marker_paths)
    marker_valid = (
        len(marker_paths) == 1
        and marker_paths[0].name == f"{paper_number}.paper.number"
        and marker_number == paper_number
    )
    if marker_present and not marker_valid:
        issues.append(EvidenceIssue("marker_mismatch", marker_number or "unreadable"))

    metadata_present = metadata_path is not None
    metadata_schema_valid = bool(metadata and not validate_metadata_schema(metadata))
    metadata_identity_valid = bool(
        metadata
        and metadata_number == metadata_raw_id
        and (not paper_number or metadata_number == paper_number)
    )
    metadata_valid = metadata_schema_valid and metadata_identity_valid
    if metadata_present and metadata is None:
        issues.append(EvidenceIssue("metadata_unreadable", metadata_path.name))
    elif metadata_present and not metadata_schema_valid:
        issues.append(EvidenceIssue("metadata_invalid", metadata_path.name))
    if metadata is not None and not metadata_identity_valid:
        issues.append(EvidenceIssue(
            "metadata_paper_number_mismatch",
            f"paper_number={metadata_number or '<empty>'};"
            f"paper_raw_id={metadata_raw_id or '<empty>'};"
            f"expected={paper_number or '<empty>'}",
        ))
    doi = metadata_doi(metadata) if metadata else ""

    parsed_sources: list[ParsedSourceRecord] = []
    source_paths = sorted((folder / "source_records").glob("metadata_source.*.json"))
    for path in source_paths:
        data = read(path)[1]
        if data is None:
            issues.append(EvidenceIssue("source_record_unreadable", path.name))
            continue
        source = data.get("source") if isinstance(data.get("source"), dict) else {}
        record = data.get("record") if isinstance(data.get("record"), dict) else {}
        contexts = (data.get("discovery_context"), record.get("discovery_context"))
        identities = tuple(identity for context in contexts if (identity := _identity_from_context(context)))
        parsed_sources.append(ParsedSourceRecord(
            path=path,
            provider=str(data.get("provider") or source.get("provider") or "").strip().lower(),
            source_kind=str(source.get("kind") or ""),
            discovery_identities=identities,
        ))
    source_records_present = bool(source_paths)
    source_records_valid = bool(parsed_sources) and len(parsed_sources) == len(source_paths)

    receipt_paths = sorted(folder.glob("*.discovery_receipt.json"))
    parsed_receipts: list[ParsedDiscoveryReceipt] = []
    for path in receipt_paths:
        data = read(path)[1]
        try:
            normalized = normalize_receipt_identity(data or {})
            identity = ParsedDiscoveryIdentity(
                provider=normalized["provider"], keyword_id=normalized["keyword_id"],
                page_id=normalized["page_id"], candidate_id=normalized["candidate_id"],
                normalized_doi=normalized["normalized_doi"],
            )
            if normalized["paper_number"] != paper_number:
                issues.append(EvidenceIssue("receipt_paper_number_mismatch", path.name))
                continue
            parsed_receipts.append(ParsedDiscoveryReceipt(path, paper_number, identity))
        except (ValueError, KeyError, TypeError):
            issues.append(EvidenceIssue("receipt_unreadable", path.name))
    receipt_present = bool(receipt_paths)
    receipt_valid = len(parsed_receipts) == 1 and len(receipt_paths) == 1

    stage_path = folder / "stage_manifest.json"
    stage_present, stage = read(stage_path)
    workflow_path = str((stage or {}).get("workflow_path") or "")
    stage_number = str((stage or {}).get("paper_number") or (stage or {}).get("paper_raw_id") or "")
    stage_valid = bool(stage and (not stage_number or stage_number == paper_number))
    if stage_present and not stage_valid:
        issues.append(EvidenceIssue("stage_manifest_unreadable" if stage is None else "stage_manifest_paper_number_mismatch"))

    asset_paths = sorted(folder.glob("*.asset_manifest.json"))
    asset_valid = any(read(path)[1] is not None for path in asset_paths)
    if asset_paths and not asset_valid:
        issues.append(EvidenceIssue("asset_manifest_unreadable"))

    status_path = folder / ".import_status.json"
    status_present, status = read(status_path)
    status_valid = False
    if status:
        if str(status.get("schema_version") or "") == "2.0":
            status_valid = bool(status.get("paper_number") or any(
                str((status.get(dim) or {}).get("state") or "")
                for dim in ("metadata", "pdf", "conversion", "catalog", "formalization", "commit")
            ))
        else:
            status_valid = bool(status.get("status") or status.get("stage"))
    if status_present and not status_valid:
        issues.append(EvidenceIssue("import_status_unreadable"))

    ledger_state = str((ledger_item or {}).get("state") or "") or None
    formal_catalog_path = folder / f"{folder.name}.catalog.json"
    formal_manifest_path = folder / f"{folder.name}.asset_manifest.json"
    formal_catalog_present = ledger_state == "active" and formal_catalog_path.is_file()
    formal_manifest_present = ledger_state == "active" and formal_manifest_path.is_file()
    formal_catalog = read(formal_catalog_path)[1] if formal_catalog_present else None
    formal_manifest = read(formal_manifest_path)[1] if formal_manifest_present else None
    ledger_folder_matches = True
    stored = str((ledger_item or {}).get("folder_path") or "")
    if stored:
        from src.path_utils import resolve_stored_path
        ledger_folder_matches = resolve_stored_path(stored).resolve() == folder.resolve()
        if not ledger_folder_matches:
            issues.append(EvidenceIssue("ledger_folder_path_mismatch"))

    first_identity = parsed_receipts[0].identity if parsed_receipts else None
    return WorkspaceEvidence(
        paper_number=paper_number, folder=folder,
        marker_present=marker_present, marker_valid=marker_valid,
        metadata_present=metadata_present, metadata_valid=metadata_valid,
        source_records_present=source_records_present, source_records_valid=source_records_valid,
        discovery_receipt_present=receipt_present, discovery_receipt_valid=receipt_valid,
        pdf_present=any(folder.glob("*.pdf")),
        asset_manifest_present=bool(asset_paths), asset_manifest_valid=asset_valid,
        stage_manifest_present=stage_present, stage_manifest_valid=stage_valid,
        import_status_present=status_present, import_status_valid=status_valid,
        ledger_folder_matches=ledger_folder_matches, ledger_state=ledger_state,
        marker_schema_version=str((marker_payload or {}).get("schema_version") or ""),
        marker_state=str((marker_payload or {}).get("state") or ""),
        marker_folder_name=str((marker_payload or {}).get("folder_name") or ""),
        marker_planned_paper_name=str((marker_payload or {}).get("planned_paper_name") or ""),
        metadata=metadata, metadata_path=metadata_path, normalized_doi=doi,
        formal_catalog_present=formal_catalog_present, formal_catalog=formal_catalog,
        formal_asset_manifest_present=formal_manifest_present,
        formal_asset_manifest=formal_manifest,
        source_kind=parsed_sources[0].source_kind if parsed_sources else "",
        source_provider=parsed_sources[0].provider if parsed_sources else "",
        source_records=tuple(parsed_sources), discovery_receipts=tuple(parsed_receipts),
        discovery_identity=(vars(first_identity) if first_identity else None),
        workflow_path=workflow_path, issues=tuple(issues),
    )
