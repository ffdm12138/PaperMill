"""Pure read-only duplicate inspection shared by formalize and commit."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from loguru import logger

from src.utils.jsonio import read_json_strict
from src.utils.identifiers import normalize_doi
from src.utils.file_fingerprint import compute_file_hashes, compute_sha256
from src.ingest.workspace import PaperRawWorkspace


@dataclass(frozen=True)
class DuplicateInspectionResult:
    status: str
    duplicate_keys: tuple[str, ...]
    findings: tuple[dict[str, Any], ...]
    ledger_snapshot_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _canonical_json_sha(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _read_json(path: Path) -> dict:
    value = read_json_strict(path)
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _metadata_keys(metadata: dict) -> dict[str, str]:
    identifiers = metadata.get("identifiers") if isinstance(metadata.get("identifiers"), dict) else {}
    keys: dict[str, str] = {}
    doi = normalize_doi(str(identifiers.get("doi") or ""))
    if doi:
        keys["doi"] = doi
    for kind, value in identifiers.items():
        normalized = str(value or "").strip().casefold()
        if kind != "doi" and normalized:
            keys[f"identifier:{str(kind).casefold()}"] = normalized
    return keys


def _markdown_fingerprint(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="ignore")
    canonical = re.sub(r"\s+", " ", text).strip().casefold().encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _asset_keys(metadata_path: Path, pdf_path: Path | None, markdown_path: Path | None) -> dict[str, str]:
    keys = _metadata_keys(_read_json(metadata_path))
    if pdf_path and pdf_path.is_file():
        hashes = compute_file_hashes(pdf_path)
        keys["pdf_sha256"] = str(hashes["sha256"])
        keys["pdf_md5"] = str(hashes["md5"]).casefold()
    if markdown_path and markdown_path.is_file():
        keys["markdown_fingerprint"] = _markdown_fingerprint(markdown_path)
    return keys


def _load_ledger(ledger: object) -> dict:
    if isinstance(ledger, dict):
        return ledger
    if hasattr(ledger, "load"):
        return ledger.load()
    path = Path(ledger)
    return _read_json(path) if path.is_file() else {"items": {}}


def inspect_ingest_duplicates(
    workspace: PaperRawWorkspace,
    *,
    ledger: object,
    papers_root: Path,
) -> DuplicateInspectionResult:
    """Inspect all duplicate identities without creating or modifying evidence."""
    metadata = _read_json(workspace.metadata)
    catalog = _read_json(workspace.catalog)
    current_keys = _asset_keys(workspace.metadata, workspace.pdf, workspace.markdown)
    paper_name = str(catalog.get("paper_name") or "")
    findings: list[dict[str, Any]] = []
    duplicate_keys: set[str] = set()
    ledger_data = _load_ledger(ledger)

    ledger_item = ((ledger_data.get("items") or {}).get(workspace.paper_number) or {})
    ledger_state = str(ledger_item.get("state") or "")
    ledger_paper_name = str(ledger_item.get("paper_name") or "")
    if ledger_state == "active" and ledger_paper_name == paper_name:
        findings.append({"kind": "paper_number", "classification": "same_paper_number_idempotent", "paper_number": workspace.paper_number, "paper_name": paper_name})
    elif ledger_item and ledger_state not in {"reserved", "metadata_staged", "allocating"}:
        findings.append({"kind": "paper_number", "classification": "conflict", "paper_number": workspace.paper_number, "state": ledger_state})
    elif ledger_paper_name and ledger_paper_name != paper_name:
        findings.append({"kind": "paper_number", "classification": "conflict", "paper_number": workspace.paper_number, "paper_name": ledger_paper_name})

    raw_root = workspace.root.parent
    for other in sorted(path for path in raw_root.iterdir() if path.is_dir() and path != workspace.root and path.name.isdigit() and len(path.name) == 16):
        metadata_paths = sorted(other.glob("*.metadata.json"))
        if len(metadata_paths) != 1:
            continue
        other_prefix = metadata_paths[0].name.removesuffix(".metadata.json")
        try:
            other_keys = _asset_keys(metadata_paths[0], other / f"{other_prefix}.pdf", other / f"{other_prefix}.md")
        except Exception:
            findings.append({"kind": "workspace", "classification": "unverifiable", "path": str(other)})
            continue
        for kind, value in current_keys.items():
            if value and other_keys.get(kind) == value:
                duplicate_keys.add(kind)
                findings.append({"kind": kind, "classification": "duplicate_pending_workspace", "paper_number": other.name, "value": value})

    if papers_root.is_dir():
        for folder in sorted(path for path in papers_root.iterdir() if path.is_dir() and not path.name.startswith(".")):
            prefix = folder.name
            metadata_path = folder / f"{prefix}.metadata.json"
            if not metadata_path.is_file():
                findings.append({"kind": "formal_workspace", "classification": "unverifiable", "path": str(folder)})
                continue
            marker_paths = sorted(folder.glob("*.paper.number"))
            existing_number = ""
            if len(marker_paths) == 1:
                try:
                    existing_number = str(_read_json(marker_paths[0]).get("paper_number") or "")
                except Exception as exc:
                    logger.warning("duplicate-inspection marker unreadable {}: {}",
                                   marker_paths[0], exc)
            if prefix == paper_name:
                classification = "same_paper_number_idempotent" if existing_number == workspace.paper_number else "conflict"
                findings.append({"kind": "paper_name", "classification": classification, "paper_number": existing_number, "paper_name": prefix})
            try:
                other_keys = _asset_keys(metadata_path, folder / f"{prefix}.pdf", folder / f"{prefix}.md")
            except Exception:
                findings.append({"kind": "formal_workspace", "classification": "unverifiable", "path": str(folder)})
                continue
            for kind, value in current_keys.items():
                if value and other_keys.get(kind) == value:
                    duplicate_keys.add(kind)
                    classification = "same_paper_number_idempotent" if existing_number == workspace.paper_number else "duplicate_existing_formal"
                    findings.append({"kind": kind, "classification": classification, "paper_number": existing_number, "paper_name": prefix, "value": value})

    classes = {str(item.get("classification")) for item in findings}
    if "conflict" in classes:
        status = "conflict"
    elif "duplicate_pending_workspace" in classes:
        status = "duplicate_pending_workspace"
    elif "duplicate_existing_formal" in classes:
        status = "duplicate_existing_formal"
    elif "unverifiable" in classes:
        status = "unverifiable"
    elif "same_paper_number_idempotent" in classes:
        status = "same_paper_number_idempotent"
    else:
        status = "clear"
    return DuplicateInspectionResult(
        status=status,
        duplicate_keys=tuple(sorted(duplicate_keys)),
        findings=tuple(findings),
        ledger_snapshot_sha256=_canonical_json_sha(ledger_data),
    )


def duplicate_inspection_sha256(result: DuplicateInspectionResult) -> str:
    return _canonical_json_sha(result.to_dict())
