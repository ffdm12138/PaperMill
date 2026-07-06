"""Asset manifest helpers for paper_raw and formal paper folders.

metadata v2.0 is citation-only, so file paths, hashes, and conversion/fetch
asset state live in ``<prefix>.asset_manifest.json`` instead.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from src.file_fingerprint import compute_file_hashes, compute_sha256
from src.utils.atomic_io import atomic_write_json


ASSET_MANIFEST_VERSION = "1.0"


def asset_manifest_path(folder: str | Path, prefix: str) -> Path:
    return Path(folder) / f"{prefix}.asset_manifest.json"


def read_asset_manifest(folder: str | Path, prefix: str) -> dict[str, Any]:
    path = asset_manifest_path(folder, prefix)
    if not path.exists():
        return {}
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _file_entry(path: Path, *, rel_path: str | None = None, include_md5: bool = False) -> dict[str, Any]:
    entry: dict[str, Any] = {"path": rel_path or path.name}
    if path.exists() and path.is_file():
        if include_md5:
            hashes = compute_file_hashes(path)
            entry.update({
                "md5": hashes["md5"],
                "sha256": hashes["sha256"],
                "file_size": hashes["file_size"],
            })
        else:
            entry.update({
                "sha256": compute_sha256(path),
                "file_size": path.stat().st_size,
            })
    return entry


def build_asset_manifest(
    folder: str | Path,
    *,
    prefix: str,
    paper_number: str,
    paper_id: str = "",
    stage: str,
    extra_files: dict[str, Any] | None = None,
) -> dict[str, Any]:
    folder = Path(folder)
    files: dict[str, Any] = {}
    pdf = folder / f"{prefix}.pdf"
    md = folder / f"{prefix}.md"
    metadata = folder / f"{prefix}.metadata.json"
    catalog = folder / f"{prefix}.catalog.json"
    images = folder / "images"
    if pdf.exists():
        files["pdf"] = _file_entry(pdf, include_md5=True)
    if md.exists():
        files["markdown"] = _file_entry(md)
    if metadata.exists():
        files["metadata"] = {"path": metadata.name}
    if catalog.exists():
        files["catalog"] = {"path": catalog.name}
    if images.exists():
        files["images_dir"] = "images/"
    if extra_files:
        files.update(deepcopy(extra_files))
    return {
        "schema_version": ASSET_MANIFEST_VERSION,
        "paper_number": paper_number,
        "paper_id": paper_id,
        "stage": stage,
        "files": files,
    }


def write_asset_manifest(
    folder: str | Path,
    *,
    prefix: str,
    paper_number: str,
    paper_id: str = "",
    stage: str,
    extra_files: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = build_asset_manifest(
        folder,
        prefix=prefix,
        paper_number=paper_number,
        paper_id=paper_id,
        stage=stage,
        extra_files=extra_files,
    )
    path = asset_manifest_path(folder, prefix)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, manifest, indent=2)
    return manifest


def pdf_hashes_from_manifest(folder: str | Path, prefix: str) -> tuple[str, str]:
    manifest = read_asset_manifest(folder, prefix)
    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    pdf = files.get("pdf") if isinstance(files.get("pdf"), dict) else {}
    md5 = str((pdf or {}).get("md5") or "").strip().lower()
    sha256 = str((pdf or {}).get("sha256") or "").strip().lower()
    return md5, sha256
