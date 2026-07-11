"""Deterministic read-only Catalog Skill task envelopes."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re

from src.file_fingerprint import compute_sha256
from src.metadata.freeze import assert_metadata_frozen
from src.naming import sanitize_paper_id
from src.utils.atomic_io import atomic_write_json

TASK_SCHEMA_VERSION = "1.0"
CATALOG_SCHEMA_VERSION = "3.2"
SKILL_VERSION = "paper_raw_catalog_curator.v3.2"

def _load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict): raise ValueError(f"JSON object required: {path.name}")
    return value

def _paper_abstract(markdown: str) -> dict | None:
    lines = markdown.splitlines(); start = None; level = 0
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s*(abstract|摘要)\s*$", line.strip(), re.I)
        if match: start, level = index + 1, len(match.group(1)); break
    if start is None: return None
    body: list[str] = []
    for line in lines[start:]:
        heading = re.match(r"^(#{1,6})\s+", line)
        if heading and len(heading.group(1)) <= level: break
        body.append(line)
    text = "\n".join(body).strip()
    if not text: return None
    return {"origin":"paper_explicit_abstract","language":"unknown","text":text,"source_ref":{"asset":"markdown","locator_type":"section","locator":"Abstract"}}

def _source_candidates(folder: Path, markdown_path: Path, source_hashes: dict[str, str]) -> list[dict]:
    candidates: list[dict] = []
    explicit = _paper_abstract(markdown_path.read_text(encoding="utf-8", errors="ignore"))
    if explicit: candidates.append(explicit)
    for relative in sorted(source_hashes):
        record = folder / relative
        try: payload = _load(record)
        except (OSError, json.JSONDecodeError, ValueError): continue
        text = payload.get("abstract") or (payload.get("record") or {}).get("abstract")
        if not text: continue
        provenance = str(payload.get("abstract_source") or "").casefold()
        origin = "provider_author_abstract" if provenance in {"author","publisher","publication"} else "provider_unspecified_abstract"
        candidates.append({"origin":origin,"language":str(payload.get("language") or "unknown"),"text":str(text),"source_ref":{"asset":"source_record","locator_type":"json_path","locator":f"{relative}#/abstract"},"source_record_sha256":source_hashes[relative]})
    return candidates

def validate_task_envelope(
    folder: Path,
    paper_number: str,
    task: dict | None = None,
    *,
    asset_prefix: str | None = None,
) -> list[str]:
    prefix = asset_prefix or paper_number
    errors: list[str] = []; path = folder / f"{prefix}.catalog_task.json"
    if task is None:
        try: task = _load(path)
        except Exception as exc: return [f"invalid catalog task: {exc}"]
    if task.get("schema_version") != TASK_SCHEMA_VERSION: errors.append("catalog task schema mismatch")
    if task.get("catalog_schema_version") != CATALOG_SCHEMA_VERSION: errors.append("catalog task catalog schema mismatch")
    if task.get("skill_version") != SKILL_VERSION: errors.append("catalog task skill version mismatch")
    if task.get("paper_number") != paper_number: errors.append("catalog task paper_number mismatch")
    try: freeze = assert_metadata_frozen(folder, paper_number, asset_prefix=asset_prefix)
    except Exception as exc: return errors + [f"metadata freeze closure invalid: {exc}"]
    hashes = task.get("input_hashes") if isinstance(task.get("input_hashes"), dict) else {}
    expected_files = {"metadata_sha256":folder/f"{prefix}.metadata.json","metadata_freeze_sha256":folder/f"{prefix}.metadata_freeze.json","markdown_sha256":folder/f"{prefix}.md","conversion_manifest_sha256":folder/f"{prefix}.conversion.json"}
    for key, asset in expected_files.items():
        if not asset.is_file(): errors.append(f"catalog task input missing: {asset.name}")
        elif hashes.get(key) != compute_sha256(asset): errors.append(f"catalog task stale: {key}")
    if hashes.get("metadata_sha256") != freeze.get("metadata_sha256"): errors.append("catalog task metadata/freeze hash mismatch")
    current_images = {p.relative_to(folder).as_posix():compute_sha256(p) for p in sorted((folder/"images").rglob("*")) if p.is_file()} if (folder/"images").is_dir() else None
    if current_images is None: errors.append("catalog task images directory is missing")
    elif hashes.get("image_hashes") != current_images: errors.append("catalog task stale: image hashes")
    source_hashes = hashes.get("source_record_hashes") or {}
    if source_hashes != freeze.get("source_record_hashes"): errors.append("catalog task source-record closure mismatch")
    for relative, expected in source_hashes.items():
        target = folder / relative
        if not target.is_file() or compute_sha256(target) != expected: errors.append(f"catalog task stale source record: {relative}")
    return errors

def build_task_envelope(folder: Path, paper_number: str) -> dict:
    freeze = assert_metadata_frozen(folder, paper_number)
    metadata_path=folder/f"{paper_number}.metadata.json"; markdown=folder/f"{paper_number}.md"; conversion=folder/f"{paper_number}.conversion.json"; images=folder/"images"
    if not markdown.is_file() or not conversion.is_file() or not images.is_dir(): raise ValueError("conversion manifest, Markdown and images are required")
    metadata=_load(metadata_path); conversion_data=_load(conversion)
    if conversion_data.get("pdf_sha256") and conversion_data.get("pdf_sha256") != freeze.get("pdf_sha256"): raise ValueError("conversion manifest PDF hash does not match frozen PDF")
    if conversion_data.get("markdown_sha256") and conversion_data.get("markdown_sha256") != compute_sha256(markdown): raise ValueError("conversion manifest Markdown hash mismatch")
    year=str(metadata.get("year") or ""); first=str((metadata.get("first_author") or {}).get("family") or "").strip(); author_token=sanitize_paper_id(first)
    if not year.isdigit() or not first or author_token == "untitled": raise ValueError("frozen metadata requires year and first_author.family")
    source_hashes=dict(freeze.get("source_record_hashes") or {})
    task={"schema_version":TASK_SCHEMA_VERSION,"catalog_schema_version":CATALOG_SCHEMA_VERSION,"skill_version":SKILL_VERSION,"paper_number":paper_number,"paper_id_prefix":f"{year}_{author_token}_","metadata_readonly_path":metadata_path.name,"metadata_freeze_path":f"{paper_number}.metadata_freeze.json","markdown_path":markdown.name,"images_dir":"images/","conversion_manifest_path":conversion.name,"source_abstract_candidates":_source_candidates(folder,markdown,source_hashes),"input_hashes":{"metadata_sha256":compute_sha256(metadata_path),"metadata_freeze_sha256":compute_sha256(folder/f"{paper_number}.metadata_freeze.json"),"markdown_sha256":compute_sha256(markdown),"conversion_manifest_sha256":compute_sha256(conversion),"image_hashes":{p.relative_to(folder).as_posix():compute_sha256(p) for p in sorted(images.rglob("*")) if p.is_file()},"source_record_hashes":source_hashes},"generated_at":datetime.now().astimezone().isoformat(timespec="seconds")}
    errors=validate_task_envelope(folder,paper_number,task)
    if errors: raise ValueError("; ".join(errors))
    return task

def write_task_envelope(folder: Path, paper_number: str) -> Path:
    path=folder/f"{paper_number}.catalog_task.json"; atomic_write_json(path,build_task_envelope(folder,paper_number),indent=2); return path
