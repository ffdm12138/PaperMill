"""Shared duplicate gates for ingest entrypoints.

The guard is intentionally independent from v2_library so allocator methods can
use it without circular imports.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from config.settings import PAPER_RAW_DIR, PAPERS_DIR
from src.discovery.models import normalize_doi
from src.file_fingerprint import compute_file_hashes
from src.path_utils import normalize_repo_path
from src.services.ingest_ids import PAPER_NUMBER_RE
from src.services.asset_manifest import pdf_hashes_from_manifest
from src.services.stage_manifest import staged_pdf_hashes


@dataclass(frozen=True)
class DuplicateRef:
    scope: str
    paper_number: str
    paper_id: str
    folder: str
    source: str
    doi: str = ""
    pdf_md5: str = ""
    pdf_sha256: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class DuplicateCheckResult:
    duplicate: bool
    blocking: bool
    reasons: list[str]
    refs: list[DuplicateRef]
    doi: str = ""
    pdf_md5: str = ""
    pdf_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "duplicate": self.duplicate,
            "blocking": self.blocking,
            "reasons": list(self.reasons),
            "refs": [ref.to_dict() for ref in self.refs],
            "doi": self.doi,
            "pdf_md5": self.pdf_md5,
            "pdf_sha256": self.pdf_sha256,
        }


@dataclass
class DuplicateIndex:
    doi_to_refs: dict[str, list[DuplicateRef]] = field(default_factory=dict)
    pdf_md5_to_refs: dict[str, list[DuplicateRef]] = field(default_factory=dict)
    pdf_sha256_to_refs: dict[str, list[DuplicateRef]] = field(default_factory=dict)

    def add_doi_ref(self, ref: DuplicateRef) -> None:
        doi = normalize_doi(ref.doi)
        if not doi:
            return
        self.doi_to_refs.setdefault(doi, []).append(ref)


class DuplicateIngestError(RuntimeError):
    def __init__(self, result: DuplicateCheckResult, message: str = "duplicate ingest item blocked"):
        self.result = result
        super().__init__(f"{message}: {', '.join(result.reasons)}")

    def to_dict(self) -> dict[str, Any]:
        return self.result.to_dict()


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _metadata_doi(metadata: dict) -> str:
    identifiers = metadata.get("identifiers") if isinstance(metadata.get("identifiers"), dict) else {}
    return normalize_doi((identifiers or {}).get("doi") or "")


def _stage_manifest_hashes(folder: Path) -> tuple[str, str]:
    manifest = _read_json(folder / "stage_manifest.json")
    md5, sha256 = staged_pdf_hashes(manifest)
    pdf_source = manifest.get("pdf_source") if isinstance(manifest.get("pdf_source"), dict) else {}
    md5 = str(md5 or pdf_source.get("original_md5") or manifest.get("staged_md5") or manifest.get("original_md5") or "").strip().lower()
    sha256 = str(
        sha256
        or pdf_source.get("original_sha256")
        or manifest.get("staged_sha256")
        or manifest.get("original_sha256")
        or ""
    ).strip().lower()
    return md5, sha256


def _workspace_pdf_hashes(folder: Path, metadata_prefix: str = "") -> tuple[str, str]:
    prefixes: list[str] = []
    if metadata_prefix:
        prefixes.append(metadata_prefix)
    prefixes.append(folder.name)
    for marker in sorted(folder.glob("*.metadata.json")):
        prefixes.append(marker.name.removesuffix(".metadata.json"))
    for prefix in dict.fromkeys(prefixes):
        md5, sha256 = pdf_hashes_from_manifest(folder, prefix)
        if md5 or sha256:
            return md5, sha256
    md5, sha256 = _stage_manifest_hashes(folder)
    if md5 or sha256:
        return md5, sha256
    pdf_paths = sorted(folder.glob("*.pdf"))
    if pdf_paths:
        try:
            hashes = compute_file_hashes(pdf_paths[0])
            return str(hashes["md5"]).lower(), str(hashes["sha256"]).lower()
        except OSError:
            return "", ""
    return "", ""


# Names that may appear as top-level children of ``data/paper_raw/`` but are
# never paper_raw workspaces. ``quarantine/`` is handled separately via
# ``include_quarantine``; the rest are nested-asset dirs or hidden/system dirs.
_NON_WORKSPACE_NAMES = frozenset({"output", "images", "__pycache__"})
_PAPER_NUMBER_SUFFIX = ".paper.number"


def is_paper_raw_workspace(folder: Path) -> bool:
    """Return True if ``folder`` is a paper_raw workspace that should participate
    in ingest duplicate detection.

    Accepts both strict 16-digit numbered workspaces and legacy / untitled /
    formalized workspaces named by ``paper_id``. Excludes ``quarantine/``
    (handled via ``include_quarantine``), hidden/system dirs, and nested asset
    dirs (``output`` / ``images`` / ``__pycache__``). Requires at least one
    asset marker so empty ledger-reserved folders are not indexed.
    """
    if not folder.is_dir():
        return False
    name = folder.name
    if name == "quarantine" or name.startswith("."):
        return False
    if name in _NON_WORKSPACE_NAMES:
        return False
    return bool(
        any(folder.glob("*.metadata.json"))
        or (folder / ".import_status.json").exists()
        or (folder / "stage_manifest.json").exists()
        or any(folder.glob("*" + _PAPER_NUMBER_SUFFIX))
        or any(folder.glob("*.pdf"))
        or any(folder.glob("*.md"))
    )


def _marker_paper_number(folder: Path) -> str:
    """Return the validated 16-digit number from a ``*.paper.number`` marker, or ``""``.

    Mirrors ``PaperNumberLedger.paper_number_from_marker``: the filename is
    ``<number>.paper.number`` and ``.stem`` only strips ``.number``, so the full
    ``.paper.number`` suffix must be stripped explicitly.
    """
    for marker in folder.glob("*" + _PAPER_NUMBER_SUFFIX):
        if marker.name.endswith(_PAPER_NUMBER_SUFFIX):
            candidate = marker.name[: -len(_PAPER_NUMBER_SUFFIX)]
        else:
            candidate = marker.stem
        if candidate and PAPER_NUMBER_RE.match(candidate):
            return candidate
    return ""


def resolve_paper_raw_identity(folder: Path) -> tuple[str, str]:
    """Return ``(paper_number, paper_raw_id)`` for a paper_raw workspace.

    ``paper_number`` evidence (strongest first): ``metadata.paper_number`` →
    ``metadata.paper_raw_id`` → ``*.paper.number`` marker →
    ``.import_status.json`` ``source_id`` → ``folder.name`` if 16-digit → ``""``.
    ``paper_raw_id`` is always ``folder.name`` (the on-disk identity). Never
    raises; tolerates missing/corrupt metadata.
    """
    metadata = read_best_metadata_json(folder)
    for key in ("paper_number", "paper_raw_id"):
        value = str(metadata.get(key) or "").strip()
        if value and PAPER_NUMBER_RE.match(value):
            return value, folder.name
    marker = _marker_paper_number(folder)
    if marker:
        return marker, folder.name
    import_status = _read_json(folder / ".import_status.json")
    source_id = str(import_status.get("source_id") or "").strip()
    if source_id and PAPER_NUMBER_RE.match(source_id):
        return source_id, folder.name
    if PAPER_NUMBER_RE.match(folder.name):
        return folder.name, folder.name
    return "", folder.name


def read_best_metadata_json(folder: Path) -> dict:
    """Read the workspace's metadata dict, tolerating missing/corrupt files."""
    primary = folder / f"{folder.name}.metadata.json"
    if primary.exists():
        return _read_json(primary)
    hits = sorted(folder.glob("*.metadata.json"))
    return _read_json(hits[0]) if hits else {}


def find_best_pdf(folder: Path) -> Path | None:
    """Return the workspace's primary PDF path, or ``None`` if absent."""
    primary = folder / f"{folder.name}.pdf"
    if primary.exists() and primary.is_file():
        return primary
    hits = sorted(folder.glob("*.pdf"))
    return hits[0] if hits else None


def _add_ref(index: DuplicateIndex, ref: DuplicateRef) -> None:
    if ref.doi:
        index.doi_to_refs.setdefault(ref.doi, []).append(ref)
    if ref.pdf_md5:
        index.pdf_md5_to_refs.setdefault(ref.pdf_md5, []).append(ref)
    if ref.pdf_sha256:
        index.pdf_sha256_to_refs.setdefault(ref.pdf_sha256, []).append(ref)


def _paper_raw_ref(
    folder: Path,
    identity: tuple[str, str],
    *,
    source: str,
    doi: str = "",
    md5: str = "",
    sha256: str = "",
) -> DuplicateRef:
    paper_number, paper_raw_id = identity
    return DuplicateRef(
        scope="paper_raw",
        paper_number=paper_number,
        paper_id=paper_raw_id,
        folder=normalize_repo_path(folder),
        source=source,
        doi=doi,
        pdf_md5=md5,
        pdf_sha256=sha256,
    )


def _papers_ref(folder: Path, metadata: dict, *, source: str, doi: str = "", md5: str = "", sha256: str = "") -> DuplicateRef:
    return DuplicateRef(
        scope="papers",
        paper_number=str(metadata.get("paper_number") or metadata.get("paper_raw_id") or ""),
        paper_id=folder.name,
        folder=normalize_repo_path(folder),
        source=source,
        doi=doi,
        pdf_md5=md5,
        pdf_sha256=sha256,
    )


def _index_paper_raw_workspace(index: DuplicateIndex, folder: Path, skip_paper_number: str) -> None:
    """Index a single paper_raw workspace (numbered or legacy/untitled)."""
    identity = resolve_paper_raw_identity(folder)
    paper_number, paper_raw_id = identity
    if skip_paper_number and skip_paper_number in {folder.name, paper_number, paper_raw_id}:
        return
    metadata = read_best_metadata_json(folder)
    doi = _metadata_doi(metadata)
    if doi:
        _add_ref(index, _paper_raw_ref(folder, identity, source="metadata", doi=doi))
    pdf_path = find_best_pdf(folder)
    if pdf_path is not None:
        try:
            hashes = compute_file_hashes(pdf_path)
        except OSError:
            hashes = {}
        if hashes:
            _add_ref(index, _paper_raw_ref(
                folder,
                identity,
                source="actual_pdf",
                md5=str(hashes.get("md5") or ""),
                sha256=str(hashes.get("sha256") or ""),
            ))
    else:
        md5, sha256 = _workspace_pdf_hashes(folder, folder.name)
        source = "asset_manifest_or_stage_manifest"
        if md5 or sha256:
            _add_ref(index, _paper_raw_ref(folder, identity, source=source, md5=md5, sha256=sha256))


def build_ingest_duplicate_index(
    *,
    paper_raw_dir: Path = PAPER_RAW_DIR,
    papers_dir: Path = PAPERS_DIR,
    skip_paper_number: str | None = None,
    include_quarantine: bool = False,
) -> DuplicateIndex:
    index = DuplicateIndex()
    paper_raw_dir = Path(paper_raw_dir)
    papers_dir = Path(papers_dir)
    skip_paper_number = str(skip_paper_number or "")

    if paper_raw_dir.exists():
        for folder in sorted(p for p in paper_raw_dir.iterdir() if p.is_dir()):
            if folder.name == "quarantine":
                if not include_quarantine:
                    continue
                # When included, treat each quarantined subdir as a workspace,
                # but skip the dedicated duplicate-workspaces holding area so
                # re-running cleanup is idempotent.
                for sub in sorted(p for p in folder.iterdir() if p.is_dir()):
                    if sub.name == "duplicate_workspaces":
                        continue
                    if not is_paper_raw_workspace(sub):
                        continue
                    _index_paper_raw_workspace(index, sub, skip_paper_number)
                continue
            if not is_paper_raw_workspace(folder):
                continue
            _index_paper_raw_workspace(index, folder, skip_paper_number)

    if papers_dir.exists():
        for folder in sorted(p for p in papers_dir.iterdir() if p.is_dir()):
            metadata_paths = sorted(folder.glob("*.metadata.json"))
            metadata = _read_json(metadata_paths[0]) if metadata_paths else {}
            doi = _metadata_doi(metadata)
            if doi:
                _add_ref(index, _papers_ref(folder, metadata, source="metadata", doi=doi))
            pdf_paths = sorted(folder.glob("*.pdf"))
            if pdf_paths:
                try:
                    hashes = compute_file_hashes(pdf_paths[0])
                except OSError:
                    hashes = {}
                if hashes:
                    _add_ref(index, _papers_ref(
                        folder,
                        metadata,
                        source="actual_pdf",
                        md5=str(hashes.get("md5") or ""),
                        sha256=str(hashes.get("sha256") or ""),
                    ))
            else:
                md5, sha256 = _workspace_pdf_hashes(folder, folder.name)
                if md5 or sha256:
                    _add_ref(index, _papers_ref(folder, metadata, source="asset_manifest_or_stage_manifest", md5=md5, sha256=sha256))
    return index


def build_doi_duplicate_index(
    *,
    paper_raw_dir: Path = PAPER_RAW_DIR,
    papers_dir: Path = PAPERS_DIR,
    skip_paper_number: str | None = None,
    include_quarantine: bool = False,
) -> DuplicateIndex:
    """Build a DOI-only duplicate index without reading or hashing PDFs."""
    index = DuplicateIndex()
    paper_raw_dir = Path(paper_raw_dir)
    papers_dir = Path(papers_dir)
    skip_paper_number = str(skip_paper_number or "")

    if paper_raw_dir.exists():
        for folder in sorted(p for p in paper_raw_dir.iterdir() if p.is_dir()):
            if folder.name == "quarantine":
                if not include_quarantine:
                    continue
                folders = [p for p in folder.iterdir() if p.is_dir() and p.name != "duplicate_workspaces"]
            else:
                folders = [folder]
            for candidate_folder in sorted(folders):
                if not is_paper_raw_workspace(candidate_folder):
                    continue
                identity = resolve_paper_raw_identity(candidate_folder)
                paper_number, paper_raw_id = identity
                if skip_paper_number and skip_paper_number in {candidate_folder.name, paper_number, paper_raw_id}:
                    continue
                metadata = read_best_metadata_json(candidate_folder)
                doi = _metadata_doi(metadata)
                if doi:
                    index.add_doi_ref(_paper_raw_ref(candidate_folder, identity, source="metadata", doi=doi))

    if papers_dir.exists():
        for folder in sorted(p for p in papers_dir.iterdir() if p.is_dir()):
            metadata_paths = sorted(folder.glob("*.metadata.json"))
            metadata = _read_json(metadata_paths[0]) if metadata_paths else {}
            doi = _metadata_doi(metadata)
            if doi:
                index.add_doi_ref(_papers_ref(folder, metadata, source="metadata", doi=doi))
    return index


def _unique_refs(refs: list[DuplicateRef]) -> list[DuplicateRef]:
    seen: set[tuple[str, str, str, str, str, str, str]] = set()
    out: list[DuplicateRef] = []
    for ref in refs:
        key = (ref.scope, ref.paper_number, ref.paper_id, ref.folder, ref.source, ref.doi, ref.pdf_sha256 or ref.pdf_md5)
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    return out


def check_pdf_duplicate(
    pdf_path: Path,
    *,
    paper_raw_dir: Path = PAPER_RAW_DIR,
    papers_dir: Path = PAPERS_DIR,
    skip_paper_number: str | None = None,
) -> DuplicateCheckResult:
    hashes = compute_file_hashes(Path(pdf_path))
    md5 = str(hashes["md5"]).lower()
    sha256 = str(hashes["sha256"]).lower()
    index = build_ingest_duplicate_index(
        paper_raw_dir=paper_raw_dir,
        papers_dir=papers_dir,
        skip_paper_number=skip_paper_number,
    )
    sha_refs = index.pdf_sha256_to_refs.get(sha256, [])
    md5_refs = index.pdf_md5_to_refs.get(md5, [])
    reasons: list[str] = []
    if sha_refs:
        reasons.append("pdf_sha256_duplicate")
    if md5_refs:
        reasons.append("pdf_md5_duplicate")
    if md5_refs and not sha_refs:
        if any(ref.pdf_sha256 and ref.pdf_sha256 != sha256 for ref in md5_refs):
            reasons.append("pdf_md5_collision_or_inconsistent_hash")
    refs = _unique_refs([*sha_refs, *md5_refs])
    return DuplicateCheckResult(
        duplicate=bool(refs),
        blocking=bool(refs),
        reasons=reasons,
        refs=refs,
        pdf_md5=md5,
        pdf_sha256=sha256,
    )


def check_doi_duplicate(
    doi: str,
    *,
    paper_raw_dir: Path = PAPER_RAW_DIR,
    papers_dir: Path = PAPERS_DIR,
    skip_paper_number: str | None = None,
    index: DuplicateIndex | None = None,
) -> DuplicateCheckResult:
    normalized = normalize_doi(doi or "")
    if not normalized:
        return DuplicateCheckResult(False, False, [], [], doi="")
    if index is None:
        index = build_doi_duplicate_index(
            paper_raw_dir=paper_raw_dir,
            papers_dir=papers_dir,
            skip_paper_number=skip_paper_number,
        )
    refs = _unique_refs(index.doi_to_refs.get(normalized, []))
    return DuplicateCheckResult(
        duplicate=bool(refs),
        blocking=bool(refs),
        reasons=["doi_duplicate"] if refs else [],
        refs=refs,
        doi=normalized,
    )


def check_metadata_duplicate(
    metadata: dict,
    *,
    paper_raw_dir: Path = PAPER_RAW_DIR,
    papers_dir: Path = PAPERS_DIR,
    skip_paper_number: str | None = None,
) -> DuplicateCheckResult:
    return check_doi_duplicate(
        _metadata_doi(metadata),
        paper_raw_dir=paper_raw_dir,
        papers_dir=papers_dir,
        skip_paper_number=skip_paper_number,
    )
