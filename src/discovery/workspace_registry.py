"""Authoritative, copy-on-write registry for discovery staging facts."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping

from src.discovery.workspace_index import DiscoveryIdentityRef, DiscoveryWorkspaceIndex
from src.library.formal_publication import (
    read_publication_state_header,
    validate_publication_state,
)
from src.workspace.evidence import WorkspaceEvidence, inspect_workspace_evidence
from src.workspace.readiness import WorkspaceReadiness, evaluate_metadata_staged
from src.library.paper_number_ledger import PaperNumberLedger
from src.library.paper_number_state import ALL_LEDGER_STATES, TERMINAL_LEDGER_STATES
from src.path_utils import resolve_stored_path
from src.ingest.duplicate_index import DuplicateIndex, DuplicateRef
from src.utils.identifiers import PAPER_NUMBER_RE
from src.file_fingerprint import compute_sha256
from src.discovery.staging_metrics import NullStagingMetricsObserver, StagingMetricsObserver
from config.settings import PROJECT_ROOT


Scope = Literal["paper_raw", "papers"]


class RegistryIssueCode(str, Enum):
    LEDGER_UNREADABLE = "ledger_unreadable"
    WORKSPACE_EVIDENCE_CORRUPT = "workspace_evidence_corrupt"
    METADATA_STAGED_WORKSPACE_INCOMPLETE = "metadata_staged_workspace_incomplete"
    ACTIVE_WORKSPACE_INCOMPLETE = "active_workspace_incomplete"
    IDENTITY_AMBIGUOUS = "identity_ambiguous"
    LEDGER_FOLDER_MISMATCH = "ledger_folder_mismatch"
    FORMAL_PUBLICATION_STATE_INVALID = "formal_publication_state_invalid"


@dataclass(frozen=True)
class RegistryIssue:
    code: RegistryIssueCode
    paper_number: str = ""
    detail: str = ""
    path: str = ""

    def __str__(self) -> str:
        fields = [self.code.value, self.paper_number, self.detail, self.path]
        return ":".join(value for value in fields if value)


@dataclass(frozen=True)
class DoiRegistryRef:
    paper_number: str
    scope: Scope
    workspace_path: Path
    normalized_doi: str
    metadata_path: Path | None

    def as_duplicate_ref(self) -> DuplicateRef:
        return DuplicateRef(
            scope=self.scope, paper_number=self.paper_number,
            paper_name=self.workspace_path.name if self.scope == "papers" else "",
            folder=self.workspace_path.as_posix(), source="metadata",
            workspace_kind="formal" if self.scope == "papers" else "paper_raw",
            doi=self.normalized_doi,
        )


@dataclass(frozen=True)
class FormalPrimaryRef:
    paper_number: str
    workspace_path: Path
    metadata_path: Path | None


@dataclass(frozen=True)
class FormalPublicationView:
    """Generation-bound projection of valid active formal papers."""
    generation: str
    active_numbers: frozenset[str]
    refs_by_number: Mapping[str, FormalPrimaryRef]
    doi_index: DuplicateIndex
    workspace_id_index: DiscoveryWorkspaceIndex


@dataclass(frozen=True)
class WorkspaceLifecycleProjection:
    ledger_state: str
    ledger_folder_path: str
    ledger_folder_name: str
    paper_name: str
    activated_at: str
    scope: Scope


@dataclass(frozen=True)
class WorkspaceScanRecord:
    paper_number: str
    workspace_path: Path
    scope: Scope
    evidence: WorkspaceEvidence
    readiness: WorkspaceReadiness
    doi_refs: tuple[DoiRegistryRef, ...]
    identity_refs: tuple[DiscoveryIdentityRef, ...]
    artifact_fingerprint: str
    lifecycle: WorkspaceLifecycleProjection
    publication_issues: tuple[RegistryIssue, ...] = ()

    @property
    def is_unsettled(self) -> bool:
        return (
            self.scope == "paper_raw"
            and self.evidence.ledger_state in {"allocating", "reserved"}
        )


@dataclass(frozen=True)
class WorkspaceRegistrySnapshot:
    records_by_number: Mapping[str, WorkspaceScanRecord]
    doi_index: DuplicateIndex
    workspace_id_index: DiscoveryWorkspaceIndex
    raw_doi_index: DuplicateIndex
    raw_workspace_id_index: DiscoveryWorkspaceIndex
    observed_paper_raw_max: int
    repair_backlog_numbers: frozenset[str]
    dirty_numbers: frozenset[str]
    indexed_raw_numbers: frozenset[str]
    indexed_formal_numbers: frozenset[str]
    formal_generation: str | None
    formal_revision: int | None = None

    def replace_record(self, record: WorkspaceScanRecord, *, max_number: int) -> "WorkspaceRegistrySnapshot":
        """Replace one paper's record and every derived index projection."""
        records = dict(self.records_by_number)
        is_new = record.paper_number not in records
        records[record.paper_number] = record
        if is_new:
            doi_index = self.doi_index.with_added_doi_refs(
                [ref.as_duplicate_ref() for ref in record.doi_refs])
            workspace_ids = self.workspace_id_index.with_added_refs(record.identity_refs)
            if record.scope == "paper_raw":
                raw_doi_index = self.raw_doi_index.with_added_doi_refs(
                    [ref.as_duplicate_ref() for ref in record.doi_refs])
                raw_workspace_ids = self.raw_workspace_id_index.with_added_refs(record.identity_refs)
            else:
                raw_doi_index = self.raw_doi_index
                raw_workspace_ids = self.raw_workspace_id_index
        else:
            mutable_dois = self.doi_index.copy()
            mutable_dois.remove_workspace(record.paper_number)
            for ref in record.doi_refs:
                mutable_dois.add_doi_ref(ref.as_duplicate_ref())
            doi_index = mutable_dois.freeze()
            mutable_ids = self.workspace_id_index.copy()
            mutable_ids.remove_workspace(record.paper_number)
            for ref in record.identity_refs:
                mutable_ids.add_or_merge(ref)
            workspace_ids = mutable_ids.freeze()
            mutable_raw_dois = self.raw_doi_index.copy()
            mutable_raw_ids = self.raw_workspace_id_index.copy()
            mutable_raw_dois.remove_workspace(record.paper_number)
            mutable_raw_ids.remove_workspace(record.paper_number)
            if record.scope == "paper_raw":
                for ref in record.doi_refs:
                    mutable_raw_dois.add_doi_ref(ref.as_duplicate_ref())
                for ref in record.identity_refs:
                    mutable_raw_ids.add_or_merge(ref)
            raw_doi_index = mutable_raw_dois.freeze()
            raw_workspace_ids = mutable_raw_ids.freeze()
        repair = set(self.repair_backlog_numbers)
        repair.discard(record.paper_number)
        if record.is_unsettled or _record_issues(record):
            repair.add(record.paper_number)
        dirty = set(self.dirty_numbers)
        dirty.discard(record.paper_number)
        raw = set(self.indexed_raw_numbers)
        if record.scope == "paper_raw":
            raw.add(record.paper_number)
        else:
            raw.discard(record.paper_number)
        formal = set(self.indexed_formal_numbers)
        if record.scope == "papers":
            formal.add(record.paper_number)
        else:
            formal.discard(record.paper_number)
        return WorkspaceRegistrySnapshot(
            records_by_number=MappingProxyType(records), doi_index=doi_index,
            workspace_id_index=workspace_ids, raw_doi_index=raw_doi_index,
            raw_workspace_id_index=raw_workspace_ids,
            observed_paper_raw_max=max_number,
            repair_backlog_numbers=frozenset(repair), dirty_numbers=frozenset(dirty),
            indexed_raw_numbers=frozenset(raw),
            indexed_formal_numbers=frozenset(formal), formal_generation=self.formal_generation,
            formal_revision=self.formal_revision,
        )


WorkspaceRegistry = WorkspaceRegistrySnapshot


@dataclass(frozen=True)
class WorkspaceRegistryBuildResult:
    registry: WorkspaceRegistrySnapshot | None
    complete: bool
    scanned_numbers: tuple[str, ...] = ()
    unsettled_numbers: tuple[str, ...] = ()
    issues: tuple[RegistryIssue, ...] = ()


@dataclass(frozen=True)
class WorkspaceRegistryRefreshResult:
    status: Literal["ok", "repair_required", "retryable_failure"]
    snapshot: WorkspaceRegistrySnapshot | None
    scanned_numbers: tuple[str, ...] = ()
    issues: tuple[RegistryIssue, ...] = ()

    @property
    def complete(self) -> bool:
        return self.status == "ok"

    @property
    def registry(self) -> WorkspaceRegistrySnapshot | None:
        return self.snapshot


@dataclass(frozen=True)
class MatchedRecordValidationResult:
    status: Literal["ok", "repair_required"]
    snapshot: WorkspaceRegistrySnapshot | None
    scanned_numbers: tuple[str, ...] = ()
    issues: tuple[RegistryIssue, ...] = ()


def _ledger_issue(detail: str, paper_number: str = "") -> RegistryIssue:
    return RegistryIssue(RegistryIssueCode.LEDGER_UNREADABLE, paper_number, detail)


def validate_ledger_view(data: Any) -> tuple[dict[str, Any] | None, list[RegistryIssue]]:
    """Validate an already-loaded ledger view without performing I/O."""
    issues: list[RegistryIssue] = []
    if not isinstance(data, dict):
        return None, [_ledger_issue("root_not_mapping")]
    items = data.get("items")
    if not isinstance(items, dict):
        issues.append(_ledger_issue("items_not_mapping"))
    max_number = str(data.get("max_number") or "")
    if not PAPER_NUMBER_RE.fullmatch(max_number):
        issues.append(_ledger_issue("invalid_max_number"))
    elif isinstance(items, dict):
        valid_item_numbers = [
            str(number) for number in items
            if PAPER_NUMBER_RE.fullmatch(str(number))
        ]
        if valid_item_numbers and max(valid_item_numbers) > max_number:
            issues.append(_ledger_issue(
                "max_number_below_item_floor", max(valid_item_numbers)))
    for number, item in (items.items() if isinstance(items, dict) else ()):
        if not PAPER_NUMBER_RE.fullmatch(str(number)):
            issues.append(_ledger_issue("invalid_paper_number", str(number)))
            continue
        if not isinstance(item, dict):
            issues.append(_ledger_issue("invalid_item", str(number)))
            continue
        state = str(item.get("state") or "")
        if state not in ALL_LEDGER_STATES:
            issues.append(_ledger_issue(f"invalid_state:{state}", str(number)))
        stored = str(item.get("folder_path") or "")
        if state not in TERMINAL_LEDGER_STATES and not stored:
            issues.append(RegistryIssue(
                RegistryIssueCode.LEDGER_FOLDER_MISMATCH, str(number), "folder_missing"))
        elif "\x00" in stored:
            issues.append(RegistryIssue(
                RegistryIssueCode.LEDGER_FOLDER_MISMATCH, str(number),
                "folder_invalid:nul"))
    return (None, issues) if issues else (data, [])


def _load_ledger(ledger: PaperNumberLedger) -> tuple[dict[str, Any] | None, list[RegistryIssue]]:
    try:
        if ledger.path.exists():
            data = json.loads(ledger.path.read_text(encoding="utf-8"))
        else:
            data = ledger.empty_data()
    except Exception as exc:
        return None, [_ledger_issue(type(exc).__name__)]
    return validate_ledger_view(data)


def _identity_refs(evidence: WorkspaceEvidence, scope: Scope) -> tuple[DiscoveryIdentityRef, ...]:
    refs: list[DiscoveryIdentityRef] = []
    for source in evidence.source_records:
        for identity in source.discovery_identities:
            refs.append(DiscoveryIdentityRef(
                paper_number=evidence.paper_number, scope=scope, workspace_path=evidence.folder,
                provider=identity.provider, keyword_id=identity.keyword_id,
                page_id=identity.page_id, candidate_id=identity.candidate_id,
                normalized_doi=identity.normalized_doi, source_record_path=source.path,
            ))
    for receipt in evidence.discovery_receipts:
        identity = receipt.identity
        refs.append(DiscoveryIdentityRef(
            paper_number=evidence.paper_number, scope=scope, workspace_path=evidence.folder,
            provider=identity.provider, keyword_id=identity.keyword_id,
            page_id=identity.page_id, candidate_id=identity.candidate_id,
            normalized_doi=identity.normalized_doi, receipt_path=receipt.path,
        ))
    return tuple(refs)


def workspace_artifact_fingerprint(folder: Path,
                                   observer: StagingMetricsObserver | None = None) -> str:
    """Cheap change detector for one workspace's direct and source-record assets."""
    (observer or NullStagingMetricsObserver()).workspace_fingerprint()
    facts: list[str] = []
    pending = [(folder, "")]
    while pending:
        parent, prefix = pending.pop()
        try:
            entries = sorted(os.scandir(parent), key=lambda entry: entry.name)
        except OSError as exc:
            facts.append(f"{prefix}!{type(exc).__name__}")
            continue
        for entry in entries:
            relative = f"{prefix}{entry.name}"
            try:
                stat = entry.stat(follow_symlinks=False)
                facts.append(f"{relative}:{stat.st_mode}:{stat.st_size}:{stat.st_mtime_ns}")
            except OSError as exc:
                facts.append(f"{relative}!{type(exc).__name__}")
                continue
            if entry.name == "source_records" and entry.is_dir(follow_symlinks=False):
                pending.append((Path(entry.path), "source_records/"))
    return hashlib.sha256("\n".join(facts).encode()).hexdigest()


def _lifecycle_projection(item: Mapping[str, Any]) -> WorkspaceLifecycleProjection:
    state = str(item.get("state") or "")
    return WorkspaceLifecycleProjection(
        ledger_state=state,
        ledger_folder_path=str(item.get("folder_path") or ""),
        ledger_folder_name=str(item.get("folder_name") or ""),
        paper_name=str(item.get("paper_name") or ""),
        activated_at=str(item.get("activated_at") or ""),
        scope="papers" if state == "active" else "paper_raw",
    )


def formal_publication_view(snapshot: WorkspaceRegistrySnapshot) -> FormalPublicationView:
    doi_index = DuplicateIndex()
    identities = DiscoveryWorkspaceIndex()
    refs: dict[str, FormalPrimaryRef] = {}
    for number in sorted(snapshot.indexed_formal_numbers):
        record = snapshot.records_by_number[number]
        refs[number] = FormalPrimaryRef(number, record.workspace_path,
                                        record.evidence.metadata_path)
        for doi_ref in record.doi_refs:
            doi_index.add_doi_ref(doi_ref.as_duplicate_ref())
        for identity_ref in record.identity_refs:
            identities.add_or_merge(identity_ref)
    return FormalPublicationView(
        snapshot.formal_generation or "", frozenset(refs), MappingProxyType(refs),
        doi_index.freeze(), identities.freeze())


def _formal_publication_identity_issues(*, folder: Path, number: str,
                                        lifecycle: WorkspaceLifecycleProjection,
                                        evidence: WorkspaceEvidence) -> tuple[RegistryIssue, ...]:
    """Validate the lightweight identity closure emitted by formal commit."""
    issues: list[RegistryIssue] = []

    def add(detail: str) -> None:
        issues.append(RegistryIssue(
            RegistryIssueCode.ACTIVE_WORKSPACE_INCOMPLETE,
            number, detail, folder.as_posix()))

    paper_name = lifecycle.paper_name
    canonical_metadata = folder / f"{paper_name}.metadata.json" if paper_name else None
    if canonical_metadata is None or evidence.metadata_path != canonical_metadata:
        add("formal_metadata_path_mismatch")

    if evidence.marker_schema_version != "1.0":
        add("formal_marker_schema_mismatch")
    if evidence.marker_state != "active":
        add("formal_marker_state_mismatch")
    if (
        not paper_name
        or evidence.marker_folder_name != paper_name
        or evidence.marker_planned_paper_name != paper_name
    ):
        add("formal_marker_name_mismatch")

    catalog = evidence.formal_catalog
    if not evidence.formal_catalog_present:
        add("formal_catalog_missing")
    elif catalog is None:
        add("formal_catalog_unreadable")
    elif catalog.get("paper_number") != number or catalog.get("paper_name") != paper_name:
        add("formal_catalog_identity_mismatch")

    manifest = evidence.formal_asset_manifest
    if not evidence.formal_asset_manifest_present:
        add("formal_asset_manifest_missing")
    elif manifest is None:
        add("formal_asset_manifest_unreadable")
    else:
        if manifest.get("schema_version") != "2.0" or manifest.get("stage") != "papers":
            add("formal_asset_manifest_publication_mismatch")
        if manifest.get("paper_number") != number or manifest.get("paper_name") != paper_name:
            add("formal_asset_manifest_identity_mismatch")
        asset_hashes = manifest.get("asset_hashes")
        expected_metadata_hash = (
            str(asset_hashes.get("metadata") or "")
            if isinstance(asset_hashes, dict) else "")
        if expected_metadata_hash and canonical_metadata is not None:
            try:
                actual_metadata_hash = compute_sha256(canonical_metadata)
            except OSError:
                actual_metadata_hash = ""
            if actual_metadata_hash != expected_metadata_hash:
                add("formal_metadata_immutable_hash_mismatch")
        files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
        if isinstance(asset_hashes, dict):
            for key, relative in files.items():
                if str(key).endswith("_dir"):
                    continue
                expected_hash = str(asset_hashes.get(key) or "")
                if not expected_hash:
                    continue
                asset = folder / str(relative)
                try:
                    actual_hash = compute_sha256(asset)
                except OSError:
                    actual_hash = ""
                if actual_hash != expected_hash:
                    add(f"formal_asset_hash_mismatch:{key}")
    return tuple(issues)


def _scan(folder: Path, number: str, scope: Scope, item: dict[str, Any],
          observer: StagingMetricsObserver | None = None) -> WorkspaceScanRecord:
    evidence = inspect_workspace_evidence(
        folder, ledger_item=item, expected_paper_number=number)
    readiness = evaluate_metadata_staged(evidence)
    lifecycle = _lifecycle_projection(item)
    doi_refs = ()
    if evidence.normalized_doi:
        doi_refs = (DoiRegistryRef(number, scope, folder, evidence.normalized_doi, evidence.metadata_path),)
    return WorkspaceScanRecord(
        number, folder, scope, evidence, readiness, doi_refs,
        _identity_refs(evidence, scope), workspace_artifact_fingerprint(folder, observer),
        lifecycle, _formal_publication_identity_issues(
            folder=folder, number=number, lifecycle=lifecycle, evidence=evidence)
        if scope == "papers" else ())


def scan_workspace_record(folder: Path, number: str, scope: Scope,
                          item: dict[str, Any], observer: StagingMetricsObserver | None = None) -> WorkspaceScanRecord:
    """Scan one workspace for write-after-read validation and direct publish."""
    return _scan(folder, number, scope, item, observer)


def _target_folder(*, item: Mapping[str, Any], scope: Scope,
                   raw_root: Path, formal_root: Path,
                   project_root: Path = PROJECT_ROOT) -> tuple[Path, Path]:
    folder = resolve_stored_path(
        str(item.get("folder_path") or ""), project_root=project_root
    )
    return folder, formal_root if scope == "papers" else raw_root


def classify_record_issues(*, ledger_state: str, evidence: WorkspaceEvidence,
                           readiness: WorkspaceReadiness) -> tuple[RegistryIssue, ...]:
    """Classify one scan using the sole ledger-state/readiness policy."""
    issues: list[RegistryIssue] = []
    corrupt_prefixes = ("metadata_unreadable", "metadata_paper_number_mismatch",
                        "source_record_unreadable", "receipt_unreadable",
                        "receipt_paper_number_mismatch", "marker_mismatch",
                        "stage_manifest_unreadable", "stage_manifest_paper_number_mismatch",
                        "asset_manifest_unreadable", "import_status_unreadable",
                        "ledger_folder_path_mismatch")
    for issue in evidence.issues:
        if issue.category.startswith(corrupt_prefixes):
            code = (RegistryIssueCode.LEDGER_FOLDER_MISMATCH
                    if issue.category == "ledger_folder_path_mismatch"
                    else RegistryIssueCode.WORKSPACE_EVIDENCE_CORRUPT)
            issues.append(RegistryIssue(
                code, evidence.paper_number, str(issue), evidence.folder.as_posix()))
    if ledger_state in {"allocating", "reserved"}:
        if readiness.profile is None and evidence.stage_manifest_present:
            issues.append(RegistryIssue(
                RegistryIssueCode.WORKSPACE_EVIDENCE_CORRUPT,
                evidence.paper_number, "unknown_workflow_path", evidence.folder.as_posix()))
    elif ledger_state == "metadata_staged" and not readiness.ready:
        issues.append(RegistryIssue(
            RegistryIssueCode.METADATA_STAGED_WORKSPACE_INCOMPLETE,
            evidence.paper_number, ",".join(readiness.missing), evidence.folder.as_posix()))
    elif ledger_state == "active":
        formal_ready = (
            evidence.marker_valid and evidence.metadata_valid
            and evidence.ledger_folder_matches
        )
        if not formal_ready:
            issues.append(RegistryIssue(
                RegistryIssueCode.ACTIVE_WORKSPACE_INCOMPLETE,
                evidence.paper_number, "formal_evidence_incomplete", evidence.folder.as_posix()))
    return tuple(issues)


def _record_issues(record: WorkspaceScanRecord) -> tuple[RegistryIssue, ...]:
    issues = list(record.publication_issues)
    issues.extend(classify_record_issues(
        ledger_state=str(record.evidence.ledger_state or ""),
        evidence=record.evidence, readiness=record.readiness))
    if record.lifecycle.ledger_state == "active":
        paper_name = record.lifecycle.paper_name
        folder_name = record.lifecycle.ledger_folder_name
        if not paper_name or not folder_name:
            detail = "active_formal_name_missing"
        elif paper_name != folder_name:
            detail = "active_formal_ledger_name_mismatch"
        elif record.workspace_path.name != paper_name:
            detail = "active_formal_directory_name_mismatch"
        else:
            detail = ""
        if detail:
            issues.append(RegistryIssue(
                RegistryIssueCode.LEDGER_FOLDER_MISMATCH,
                record.paper_number, detail, record.workspace_path.as_posix()))
    return tuple(issues)


def _snapshot(
    records: dict[str, WorkspaceScanRecord], max_number: int,
    *, formal_generation: str | None = None, formal_revision: int | None = None,
) -> WorkspaceRegistrySnapshot:
    doi_index = DuplicateIndex()
    workspace_ids = DiscoveryWorkspaceIndex()
    raw_doi_index = DuplicateIndex()
    raw_workspace_ids = DiscoveryWorkspaceIndex()
    repair: set[str] = set()
    raw: set[str] = set()
    formal: set[str] = set()
    for number, record in sorted(records.items()):
        for ref in record.doi_refs:
            doi_index.add_doi_ref(ref.as_duplicate_ref())
            if record.scope == "paper_raw":
                raw_doi_index.add_doi_ref(ref.as_duplicate_ref())
        for ref in record.identity_refs:
            workspace_ids.add_or_merge(ref)
            if record.scope == "paper_raw":
                raw_workspace_ids.add_or_merge(ref)
        if record.scope == "paper_raw":
            raw.add(number)
        if record.is_unsettled or _record_issues(record):
            repair.add(number)
        if record.scope == "papers":
            formal.add(number)
    return WorkspaceRegistrySnapshot(
        records_by_number=MappingProxyType(dict(records)), doi_index=doi_index.freeze(),
        workspace_id_index=workspace_ids.freeze(), raw_doi_index=raw_doi_index.freeze(),
        raw_workspace_id_index=raw_workspace_ids.freeze(), observed_paper_raw_max=max_number,
        repair_backlog_numbers=frozenset(repair), dirty_numbers=frozenset(),
        indexed_raw_numbers=frozenset(raw),
        indexed_formal_numbers=frozenset(formal), formal_generation=formal_generation,
        formal_revision=formal_revision,
    )


def build_workspace_registry(*, paper_raw_dir: str | Path, papers_dir: str | Path,
                             ledger: PaperNumberLedger,
                             observer: StagingMetricsObserver | None = None,
                             project_root: Path = PROJECT_ROOT) -> WorkspaceRegistryBuildResult:
    observer = observer or NullStagingMetricsObserver()
    observer.registry_full_build()
    observer.formal_publication_view_load()
    observer.ledger_load()
    data, issues = _load_ledger(ledger)
    if data is None:
        return WorkspaceRegistryBuildResult(None, False, issues=tuple(issues))
    raw_root, formal_root = Path(paper_raw_dir), Path(papers_dir)
    records: dict[str, WorkspaceScanRecord] = {}
    scanned: list[str] = []
    items: dict[str, dict[str, Any]] = data["items"]
    ledger_numbers = set(items)
    disk_raw = {
        p.name for p in raw_root.iterdir()
        if p.is_dir() and not p.is_symlink() and PAPER_NUMBER_RE.fullmatch(p.name)
    } if raw_root.exists() else set()
    for number in sorted(disk_raw - ledger_numbers):
        issues.append(RegistryIssue(
            RegistryIssueCode.LEDGER_FOLDER_MISMATCH, number, "untracked_paper_raw"))
    active_paths = {
        resolve_stored_path(
            str(item.get("folder_path") or ""), project_root=Path(project_root)
        ).resolve()
        for item in items.values()
        if isinstance(item, dict) and str(item.get("state") or "") == "active"
        and str(item.get("folder_path") or "")
    }
    if formal_root.exists():
        for folder in (
            path for path in formal_root.iterdir()
            if path.is_dir() and not path.is_symlink() and not path.name.startswith(".")
        ):
            if folder.resolve() not in active_paths:
                issues.append(RegistryIssue(
                    RegistryIssueCode.LEDGER_FOLDER_MISMATCH, "",
                    "untracked_formal", folder.as_posix()))
    for number, item in sorted(items.items()):
        state = str(item.get("state") or "")
        if state in TERMINAL_LEDGER_STATES:
            continue
        stored = str(item.get("folder_path") or "")
        folder = resolve_stored_path(stored, project_root=Path(project_root))
        expected_root = formal_root if state == "active" else raw_root
        try:
            if folder.resolve(strict=False).parent != expected_root.resolve(strict=False):
                issues.append(RegistryIssue(
                    RegistryIssueCode.LEDGER_FOLDER_MISMATCH, number,
                    "workspace_outside_expected_root", folder.as_posix(),
                ))
                continue
        except OSError:
            issues.append(RegistryIssue(
                RegistryIssueCode.LEDGER_FOLDER_MISMATCH, number,
                "workspace_path_unreadable", folder.as_posix(),
            ))
            continue
        scope: Scope = "papers" if state == "active" else "paper_raw"
        expected_root = formal_root if scope == "papers" else raw_root
        if folder.is_symlink() or not folder.is_dir() or folder.parent.resolve() != expected_root.resolve():
            issues.append(RegistryIssue(
                RegistryIssueCode.LEDGER_FOLDER_MISMATCH, number,
                "workspace_mismatch", folder.as_posix()))
            continue
        record = _scan(folder, number, scope, item, observer)
        observer.workspace_record_read(unsettled=record.is_unsettled)
        if record.paper_number != number:
            issues.append(RegistryIssue(
                RegistryIssueCode.WORKSPACE_EVIDENCE_CORRUPT, number,
                "paper_number_mismatch", folder.as_posix()))
        issues.extend(_record_issues(record))
        records[number] = record
        scanned.append(number)
    # Fatal ledger issues (e.g. unreadable ledger) already returned above.
    # Remaining issues are per-workspace and non-fatal: build the registry
    # from successfully scanned workspaces, keeping issues for diagnostics.
    publication = validate_publication_state(
        papers_dir=formal_root, ledger_items=items, project_root=Path(project_root),
    )
    for detail in publication.issues:
        issues.append(RegistryIssue(
            RegistryIssueCode.FORMAL_PUBLICATION_STATE_INVALID,
            "", detail, str(formal_root / ".formal_publication_state.json"),
        ))
    max_number = int(data["max_number"])
    snapshot = _snapshot(
        records, max_number,
        formal_generation=publication.generation if publication.valid else None,
        formal_revision=publication.revision if publication.valid else None,
    )
    fatal = any(issue.code in {
        RegistryIssueCode.WORKSPACE_EVIDENCE_CORRUPT,
        RegistryIssueCode.METADATA_STAGED_WORKSPACE_INCOMPLETE,
        RegistryIssueCode.ACTIVE_WORKSPACE_INCOMPLETE,
        RegistryIssueCode.LEDGER_FOLDER_MISMATCH,
        RegistryIssueCode.FORMAL_PUBLICATION_STATE_INVALID,
    } for issue in issues)
    if fatal:
        # Preserve a diagnostic/repair-only projection of independently valid
        # workspaces.  Discovery still fails closed on ``complete=False``;
        # explicit repair can operate on a healthy selected number without an
        # unrelated damaged workspace making recovery impossible.
        healthy_records = {
            number: record for number, record in records.items()
            if not _record_issues(record)
        }
        partial = _snapshot(healthy_records, max_number)
        return WorkspaceRegistryBuildResult(partial, False, tuple(scanned),
                                            tuple(sorted(snapshot.repair_backlog_numbers)),
                                            tuple(issues))
    return WorkspaceRegistryBuildResult(snapshot, not bool(issues), tuple(scanned),
                                        tuple(sorted(snapshot.repair_backlog_numbers)),
                                        tuple(issues))


def refresh_registry_under_write_lock(current: WorkspaceRegistrySnapshot, *,
                                      paper_raw_dir: str | Path, papers_dir: str | Path,
                                      ledger_view: Mapping[str, Any],
                                      observer: StagingMetricsObserver | None = None,
                                      dirty_numbers: set[str] | frozenset[str] = frozenset(),
                                      project_root: Path = PROJECT_ROOT) -> WorkspaceRegistryRefreshResult:
    """Build a temporary replacement and publish only a fully valid snapshot."""
    observer = observer or NullStagingMetricsObserver()
    observer.registry_pre_refresh()
    observer.registry_incremental_refresh()
    data, issues = validate_ledger_view(dict(ledger_view))
    if data is None:
        return WorkspaceRegistryRefreshResult("retryable_failure", None, issues=tuple(issues))
    records = dict(current.records_by_number)
    scanned: list[str] = []
    raw_root, formal_root = Path(paper_raw_dir), Path(papers_dir)
    items: dict[str, dict[str, Any]] = data["items"]
    current_max = int(data["max_number"])
    active_numbers = {
        str(number) for number, item in items.items()
        if isinstance(item, Mapping) and str(item.get("state") or "") == "active"
    }
    publication_header = read_publication_state_header(
        papers_dir=formal_root, active_numbers=active_numbers,
    )
    if not publication_header.valid:
        return WorkspaceRegistryRefreshResult(
            "repair_required", None,
            issues=tuple(RegistryIssue(
                RegistryIssueCode.FORMAL_PUBLICATION_STATE_INVALID,
                "", detail, str(formal_root / ".formal_publication_state.json"),
            ) for detail in publication_header.issues),
        )
    generation_changed = (
        publication_header.generation != current.formal_generation
        or publication_header.revision != current.formal_revision
    )
    if generation_changed:
        # Official commit/rollback publishes a new revision/generation.  Only
        # that supported publication event requires a full closure recheck;
        # ordinary staging epochs reuse the batch-bound formal projection.
        publication = validate_publication_state(
            papers_dir=formal_root, ledger_items=items, project_root=Path(project_root),
        )
        if not publication.valid:
            return WorkspaceRegistryRefreshResult(
                "repair_required", None,
                issues=tuple(RegistryIssue(
                    RegistryIssueCode.FORMAL_PUBLICATION_STATE_INVALID,
                    "", detail, str(formal_root / ".formal_publication_state.json"),
                ) for detail in publication.issues),
            )
        next_formal_generation = publication.generation
        next_formal_revision = publication.revision
    else:
        next_formal_generation = current.formal_generation
        next_formal_revision = current.formal_revision
    changed = (
        current_max != current.observed_paper_raw_max
        or generation_changed
    )
    # Historical repair backlog is deliberately excluded. It is probed only at
    # batch boundaries or by the explicit repair command.
    to_scan = set(current.dirty_numbers) | set(dirty_numbers)
    to_scan.update(f"{number:016d}" for number in range(current.observed_paper_raw_max + 1, current_max + 1))
    for number, previous in current.records_by_number.items():
        item = items.get(number)
        if not isinstance(item, dict):
            issues.append(_ledger_issue("item_missing", number))
            continue
        if _lifecycle_projection(item) != previous.lifecycle:
            to_scan.add(number)
    for number, item in items.items():
        if number not in current.records_by_number and str(item.get("state") or "") not in TERMINAL_LEDGER_STATES:
            to_scan.add(number)
    if generation_changed:
        to_scan.update(
            number for number, item in items.items()
            if isinstance(item, dict) and str(item.get("state") or "") == "active"
        )
        to_scan.update(current.indexed_formal_numbers)
    for number in sorted(to_scan):
        item = items.get(number)
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or "")
        if state in TERMINAL_LEDGER_STATES:
            if records.pop(number, None) is not None:
                changed = True
            continue
        projection = _lifecycle_projection(item)
        folder, expected_root = _target_folder(
            item=item, scope=projection.scope, raw_root=raw_root,
            formal_root=formal_root, project_root=project_root)
        if folder.is_symlink() or not folder.is_dir() or folder.parent.resolve() != expected_root.resolve():
            issues.append(RegistryIssue(
                RegistryIssueCode.LEDGER_FOLDER_MISMATCH, number,
                "workspace_mismatch", folder.as_posix()))
            continue
        previous = records.get(number)
        if (
            previous is not None
            and previous.workspace_path == folder
            and previous.lifecycle == projection
            and previous.artifact_fingerprint == workspace_artifact_fingerprint(folder, observer)
        ):
            continue
        record = _scan(folder, number, projection.scope, item, observer)
        observer.workspace_record_read(unsettled=record.is_unsettled)
        issues.extend(_record_issues(record))
        records[number] = record
        changed = True
        scanned.append(number)
    if issues:
        return WorkspaceRegistryRefreshResult("repair_required", None, tuple(scanned), tuple(issues))
    if not changed:
        return WorkspaceRegistryRefreshResult("ok", current, tuple(scanned), ())
    replacement = _snapshot(
        records, current_max, formal_generation=next_formal_generation,
        formal_revision=next_formal_revision,
    )
    if generation_changed:
        observer.formal_publication_view_load()
    return WorkspaceRegistryRefreshResult("ok", replacement, tuple(scanned), ())


def revalidate_matched_records(snapshot: WorkspaceRegistrySnapshot,
                               paper_numbers: set[str], *,
                               paper_raw_dir: str | Path, papers_dir: str | Path,
                               ledger_view: Mapping[str, Any],
                               observer: StagingMetricsObserver | None = None,
                               project_root: Path = PROJECT_ROOT,
                               ) -> MatchedRecordValidationResult:
    """Live-scan only records reached by the candidate's identity/DOI lookups."""
    observer = observer or NullStagingMetricsObserver()
    data, ledger_issues = validate_ledger_view(dict(ledger_view))
    if data is None:
        return MatchedRecordValidationResult(
            "repair_required", None, issues=tuple(ledger_issues))
    raw_root, formal_root = Path(paper_raw_dir), Path(papers_dir)
    replacement = snapshot
    scanned: list[str] = []
    issues: list[RegistryIssue] = []
    for number in sorted(paper_numbers):
        observer.matched_revalidation()
        item = data["items"].get(number)
        if not isinstance(item, dict):
            issues.append(_ledger_issue("item_missing", number))
            continue
        projection = _lifecycle_projection(item)
        if projection.ledger_state in TERMINAL_LEDGER_STATES:
            issues.append(_ledger_issue(f"matched_item_{projection.ledger_state}", number))
            continue
        folder, expected_root = _target_folder(
            item=item, scope=projection.scope, raw_root=raw_root,
            formal_root=formal_root, project_root=project_root)
        if folder.is_symlink() or not folder.is_dir() or folder.parent.resolve() != expected_root.resolve():
            issues.append(RegistryIssue(
                RegistryIssueCode.LEDGER_FOLDER_MISMATCH, number,
                "matched_workspace_mismatch", folder.as_posix()))
            continue
        record = _scan(folder, number, projection.scope, item, observer)
        observer.workspace_record_read(unsettled=record.is_unsettled)
        record_issues = _record_issues(record)
        if record_issues:
            issues.extend(record_issues)
            continue
        replacement = replacement.replace_record(record, max_number=int(data["max_number"]))
        scanned.append(number)
    if issues:
        return MatchedRecordValidationResult(
            "repair_required", None, tuple(scanned), tuple(issues))
    return MatchedRecordValidationResult("ok", replacement, tuple(scanned), ())
