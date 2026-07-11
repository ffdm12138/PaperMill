"""Catalog freeze receipts and full input-closure replay."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from src.catalog.schema import validate_catalog_v32
from src.catalog.task import SKILL_VERSION, validate_task_envelope
from src.file_fingerprint import compute_sha256
from src.metadata.freeze import assert_metadata_frozen
from src.utils.atomic_io import atomic_write_json

CATALOG_FREEZE_SCHEMA_VERSION="1.0"

def _load(path: Path, label: str) -> dict:
    try: value=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError) as exc: raise ValueError(f"invalid {label}: {exc}") from exc
    if not isinstance(value,dict): raise ValueError(f"{label} must be an object")
    return value

def _closure(folder: Path,paper_number: str,*,papers_dir: Path|None=None,paper_raw_root: Path|None=None,require_canonical_directory_name: bool=True)->tuple[dict,list[str]]:
    errors=[]; catalog_path=folder/f"{paper_number}.catalog.json"; task_path=folder/f"{paper_number}.catalog_task.json"
    try: metadata_freeze=assert_metadata_frozen(folder,paper_number,asset_prefix=None if require_canonical_directory_name else paper_number)
    except Exception as exc: metadata_freeze={}; errors.append(f"metadata freeze closure invalid: {exc}")
    try: catalog=_load(catalog_path,"catalog")
    except Exception as exc: return {},errors+[str(exc)]
    try: task=_load(task_path,"catalog task")
    except Exception as exc: return {},errors+[str(exc)]
    prefix = None if require_canonical_directory_name else paper_number
    errors.extend(validate_task_envelope(folder,paper_number,task,asset_prefix=prefix))
    errors.extend(validate_catalog_v32(
        catalog,folder,paper_number,papers_dir=papers_dir,paper_raw_root=paper_raw_root,
        asset_prefix=prefix,
    ))
    hashes=task.get("input_hashes") or {}
    closure={"catalog_sha256":compute_sha256(catalog_path),"catalog_task_sha256":compute_sha256(task_path),"metadata_sha256":hashes.get("metadata_sha256"),"metadata_freeze_sha256":hashes.get("metadata_freeze_sha256"),"markdown_sha256":hashes.get("markdown_sha256"),"conversion_manifest_sha256":hashes.get("conversion_manifest_sha256"),"image_hashes":hashes.get("image_hashes") or {},"source_record_hashes":hashes.get("source_record_hashes") or {},"paper_id":catalog.get("paper_id"),"skill_version":task.get("skill_version"),"catalog_schema_version":task.get("catalog_schema_version")}
    if metadata_freeze and closure["metadata_sha256"]!=metadata_freeze.get("metadata_sha256"): errors.append("catalog closure metadata hash mismatch")
    return closure,list(dict.fromkeys(errors))

def freeze_catalog(folder: Path,paper_number: str,*,papers_dir: Path|None=None,paper_raw_root: Path|None=None)->dict:
    closure,errors=_closure(folder,paper_number,papers_dir=papers_dir,paper_raw_root=paper_raw_root)
    if errors: raise ValueError("; ".join(errors))
    receipt={"schema_version":CATALOG_FREEZE_SCHEMA_VERSION,"paper_number":paper_number,**closure,"frozen_at":datetime.now().astimezone().isoformat(timespec="seconds")}
    atomic_write_json(folder/f"{paper_number}.catalog_freeze.json",receipt,indent=2); return receipt

def assert_catalog_frozen(folder: Path,paper_number: str,*,papers_dir: Path|None=None,paper_raw_root: Path|None=None,require_canonical_directory_name: bool=True)->dict:
    receipt=_load(folder/f"{paper_number}.catalog_freeze.json","catalog freeze receipt"); errors=[]
    if receipt.get("schema_version")!=CATALOG_FREEZE_SCHEMA_VERSION: errors.append("catalog freeze schema mismatch")
    if receipt.get("paper_number")!=paper_number: errors.append("catalog freeze paper_number mismatch")
    closure,closure_errors=_closure(folder,paper_number,papers_dir=papers_dir,paper_raw_root=paper_raw_root,require_canonical_directory_name=require_canonical_directory_name); errors.extend(closure_errors)
    for key,value in closure.items():
        if receipt.get(key)!=value: errors.append(f"catalog freeze closure mismatch: {key}")
    if errors: raise ValueError("; ".join(dict.fromkeys(errors)))
    return receipt


def assert_formal_catalog_frozen(folder: Path, paper_number: str, paper_id: str) -> dict:
    """Validate the installed Catalog closure without the deleted raw workspace.

    The immutable receipt keeps the generation-time task/input hashes.  Formal
    validation rechecks every input that remains authoritative after commit:
    catalog, metadata, Markdown, conversion manifest, images and source records.
    """
    receipt = _load(folder / f"{paper_id}.catalog_freeze.json", "catalog freeze receipt")
    catalog = _load(folder / f"{paper_id}.catalog.json", "catalog")
    task = _load(folder / f"{paper_id}.catalog_task.json", "catalog task")
    errors: list[str] = []
    if receipt.get("schema_version") != CATALOG_FREEZE_SCHEMA_VERSION:
        errors.append("catalog freeze schema mismatch")
    if receipt.get("paper_number") != paper_number:
        errors.append("catalog freeze paper_number mismatch")
    if receipt.get("paper_id") != paper_id:
        errors.append("catalog freeze paper_id mismatch")
    if receipt.get("catalog_sha256") != compute_sha256(folder / f"{paper_id}.catalog.json"):
        errors.append("catalog freeze catalog hash mismatch")
    if receipt.get("catalog_task_sha256") != compute_sha256(folder / f"{paper_id}.catalog_task.json"):
        errors.append("catalog freeze task hash mismatch")
    if receipt.get("metadata_sha256") != compute_sha256(folder / f"{paper_id}.metadata.json"):
        errors.append("catalog freeze metadata hash mismatch")
    if receipt.get("markdown_sha256") != compute_sha256(folder / f"{paper_id}.md"):
        errors.append("catalog freeze Markdown hash mismatch")
    if receipt.get("conversion_manifest_sha256") != compute_sha256(folder / f"{paper_id}.conversion.json"):
        errors.append("catalog freeze conversion hash mismatch")
    image_hashes = {
        path.relative_to(folder).as_posix(): compute_sha256(path)
        for path in sorted((folder / "images").rglob("*"))
        if path.is_file()
    }
    if receipt.get("image_hashes") != image_hashes:
        errors.append("catalog freeze image hashes mismatch")
    source_hashes = {
        path.relative_to(folder).as_posix(): compute_sha256(path)
        for path in sorted((folder / "source_records").rglob("*"))
        if path.is_file()
    }
    if receipt.get("source_record_hashes") != source_hashes:
        errors.append("catalog freeze source-record hashes mismatch")
    errors.extend(validate_catalog_v32(catalog, folder, paper_number, asset_prefix=paper_id))
    if task.get("paper_number") != paper_number:
        errors.append("catalog task paper_number mismatch")
    if errors:
        raise ValueError("; ".join(dict.fromkeys(errors)))
    return receipt
