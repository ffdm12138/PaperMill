"""Authoritative JSON-Schema and semantic validation for Catalog v3.2."""
from __future__ import annotations

import json
import unicodedata
from pathlib import Path
import re

import jsonschema

from src.catalog.task import SKILL_VERSION, validate_task_envelope
from src.file_fingerprint import compute_sha256
from src.naming import validate_paper_id

FORBIDDEN_BIBLIOGRAPHIC_KEYS={"doi","authors","publication_year","journal","volume","issue","pages","publisher","bibtex","csl","citation_key"}
SCHEMA_PATH=Path(__file__).resolve().parents[2]/"skills"/"paper_raw_catalog_curator"/"catalog_schema.json"
MAX_PAPER_ID_CHARS=96
DEFAULT_WINDOWS_PATH_BUDGET=240

def truncate_summary(value: str, max_chars: int = 600) -> str:
    """Bound one per-paper summary for a single screening prompt batch."""
    if len(value) <= max_chars: return value
    head=value[:max_chars-1]; boundary=max((head.rfind(mark) for mark in "。？！"),default=-1)
    return (head[:boundary+1] if boundary>=0 else head)+"…"

def load_catalog_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

def _schema_errors(catalog: dict) -> list[str]:
    validator=jsonschema.Draft7Validator(load_catalog_schema())
    return [f"catalog schema {'.'.join(str(x) for x in error.absolute_path) or '<root>'}: {error.message}" for error in sorted(validator.iter_errors(catalog),key=lambda e:list(e.absolute_path))]

def validate_catalog_document(catalog: dict) -> list[str]:
    """Validate the context-free Catalog v3.2 document contract."""
    return list(dict.fromkeys([*_schema_errors(catalog), *_recursive_forbidden(catalog)]))

def _recursive_forbidden(value: object, path: tuple[str,...]=()) -> list[str]:
    errors=[]
    if isinstance(value,dict):
        for key,child in value.items():
            current=path+(str(key),)
            if str(key).casefold() in FORBIDDEN_BIBLIOGRAPHIC_KEYS: errors.append(f"catalog.{'.'.join(current)} is a forbidden structured bibliographic field")
            errors.extend(_recursive_forbidden(child,current))
    elif isinstance(value,list):
        for index,child in enumerate(value): errors.extend(_recursive_forbidden(child,path+(str(index),)))
    return errors

def _all_evidence_refs(value: object):
    if isinstance(value,dict):
        if isinstance(value.get("evidence_refs"),list): yield from value["evidence_refs"]
        for child in value.values(): yield from _all_evidence_refs(child)
    elif isinstance(value,list):
        for child in value: yield from _all_evidence_refs(child)

def _markdown_headings(path: Path) -> set[str]:
    return {re.sub(r"\s+"," ",m.group(1).strip()).casefold() for line in path.read_text(encoding="utf-8",errors="ignore").splitlines() if (m:=re.match(r"^#{1,6}\s+(.+?)\s*$",line))}

def _validate_evidence_ref(ref: object, folder: Path, headings: set[str], markdown_text: str) -> list[str]:
    if not isinstance(ref,dict): return ["evidence_refs entries must be objects"]
    errors=[]; asset=ref.get("asset"); locator_type=ref.get("locator_type"); locator=str(ref.get("locator") or "").strip(); image=str(ref.get("image_ref") or "").strip()
    if asset not in {"markdown","image","table","pdf"}: errors.append("evidence_refs.asset is invalid")
    if locator_type not in {"section","figure","table","page"}: errors.append("evidence_refs.locator_type is invalid")
    if not locator: errors.append("evidence_refs.locator is required")
    path=Path(locator)
    if path.is_absolute() or ".." in path.parts: errors.append("evidence_refs.locator must be logical and workspace-relative")
    if locator_type=="section":
        parts=[x.strip().casefold() for x in re.split(r"[/›>]",locator) if x.strip()]
        if not parts or parts[-1] not in headings: errors.append(f"evidence section not found in Markdown: {locator}")
    if locator_type in {"figure","table"} and locator.casefold() not in markdown_text.casefold(): errors.append(f"evidence label not found in Markdown: {locator}")
    if asset=="image" and not image: errors.append("image evidence requires image_ref")
    if image:
        image_path=Path(image); target=(folder/image_path).resolve(); root=folder.resolve()
        if image_path.is_absolute() or ".." in image_path.parts or root not in (target,*target.parents): errors.append("evidence_refs.image_ref escapes workspace")
        elif not image_path.as_posix().startswith("images/"): errors.append("evidence_refs.image_ref must use images/")
        elif not target.is_file(): errors.append(f"evidence_refs.image_ref missing: {image}")
    return errors

def _validate_abstract(catalog: dict, task: dict) -> list[str]:
    errors=[]; source=catalog.get("abstract",{}).get("source",{}); origin=source.get("origin"); status=source.get("status")
    if origin=="not_found":
        if status!="not_found" or source.get("text") is not None or source.get("source_ref") is not None: errors.append("not_found abstract must have null text/source_ref and status=not_found")
    else:
        if status!="present" or not str(source.get("text") or "").strip() or not isinstance(source.get("source_ref"),dict): errors.append("present abstract requires trusted source text/reference")
        candidates=task.get("source_abstract_candidates") or []
        if not any(origin==c.get("origin") and source.get("text")==c.get("text") and source.get("source_ref")==c.get("source_ref") for c in candidates if isinstance(c,dict)): errors.append("abstract source is not a trusted task candidate")
    return errors

def validate_catalog_v32(catalog: dict, folder: Path, paper_number: str, *, papers_dir: Path|None=None, paper_raw_root: Path|None=None, path_budget: int=DEFAULT_WINDOWS_PATH_BUDGET, asset_prefix: str|None=None) -> list[str]:
    errors=_schema_errors(catalog)
    if errors: return errors+_recursive_forbidden(catalog)
    prefix=asset_prefix or paper_number
    task_path=folder/f"{prefix}.catalog_task.json"
    try: task=json.loads(task_path.read_text(encoding="utf-8"))
    except Exception as exc: return [f"catalog task required before validation: {exc}"]
    errors.extend(validate_task_envelope(folder,paper_number,task,asset_prefix=asset_prefix))
    if catalog.get("paper_number")!=paper_number: errors.append("catalog.paper_number mismatch")
    pid=str(catalog.get("paper_id") or ""); title=str((catalog.get("content_identity") or {}).get("content_title_zh") or "")
    if pid!=unicodedata.normalize("NFC",pid) or title!=unicodedata.normalize("NFC",title): errors.append("paper_id/content_title_zh must use Unicode NFC")
    if pid!=str(task.get("paper_id_prefix") or "")+title: errors.append("paper_id must equal task prefix + content_title_zh")
    try: validate_paper_id(pid)
    except ValueError as exc: errors.append(str(exc))
    if len(pid)>MAX_PAPER_ID_CHARS: errors.append("paper_id exceeds 96 characters")
    if "__" in pid or pid!=pid.strip(" ."): errors.append("paper_id contains repeated underscore or trailing/leading space/dot")
    longest=(papers_dir/f"{pid}.staging"/f"{pid}.asset_manifest.json").resolve() if papers_dir else (folder/f"{pid}.asset_manifest.json").resolve()
    if len(str(longest))>path_budget: errors.append("paper_id exceeds final Windows path budget")
    if papers_dir and (papers_dir/pid).exists():
        marker=list((papers_dir/pid).glob("*.paper.number")); existing=marker[0].name.removesuffix(".paper.number") if marker else ""
        if existing!=paper_number: errors.append("paper_id_conflict: formal library contains another paper_number")
    if paper_raw_root:
        for other in (paper_raw_root.iterdir() if paper_raw_root.is_dir() else []):
            if other==folder or not other.is_dir(): continue
            for path in other.glob("*.catalog.json"):
                try: candidate=json.loads(path.read_text(encoding="utf-8"))
                except Exception: continue
                if candidate.get("paper_id")==pid and candidate.get("paper_number")!=paper_number: errors.append("paper_id_conflict: another raw workspace uses this paper_id")
    errors.extend(_recursive_forbidden(catalog))
    terminology=catalog["terminology"]
    if not terminology["items"] and not str(terminology.get("not_applicable_reason") or "").strip(): errors.append("terminology requires items or not_applicable_reason")
    errors.extend(_validate_abstract(catalog,task))
    markdown_path=folder/f"{prefix}.md"; markdown_text=markdown_path.read_text(encoding="utf-8",errors="ignore"); headings=_markdown_headings(markdown_path)
    for ref in _all_evidence_refs(catalog): errors.extend(_validate_evidence_ref(ref,folder,headings,markdown_text))
    provenance=catalog["provenance"]; hashes=task.get("input_hashes") or {}
    expected={**hashes,"catalog_task_sha256":compute_sha256(task_path),"skill_version":SKILL_VERSION}
    for key in ("metadata_sha256","metadata_freeze_sha256","markdown_sha256","conversion_manifest_sha256","image_hashes","source_record_hashes","catalog_task_sha256","skill_version"):
        if provenance.get(key)!=expected.get(key): errors.append(f"catalog provenance mismatch: {key}")
    return list(dict.fromkeys(errors))
