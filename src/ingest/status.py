"""Nested import status v2 and asset-derived readiness inspection."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from typing import Callable

from filelock import FileLock

from src.catalog.freeze import assert_catalog_frozen
from src.ingest.workspace import PaperRawWorkspace
from src.metadata.freeze import assert_metadata_frozen
from src.utils.atomic_io import atomic_write_json_unlocked

STATUS_SCHEMA_VERSION="2.0"
ALLOWED={"metadata":{"missing","resolving","resolved","matched","frozen","invalid","mismatch"},"pdf":{"missing","fetching","attached","duplicate","failed"},"conversion":{"pending","running","complete","failed"},"catalog":{"missing","waiting_for_llm","generated","validated","invalid","frozen","stale"},"formalization":{"pending","ready","stale","failed"},"commit":{"pending","committing","failed","imported"}}

def initial_status(paper_number: str)->dict:
    return {"schema_version":STATUS_SCHEMA_VERSION,"paper_number":paper_number,"metadata":{"state":"missing","revision":0},"pdf":{"state":"missing"},"conversion":{"state":"pending"},"catalog":{"state":"missing"},"formalization":{"state":"pending"},"commit":{"state":"pending"},"updated_at":datetime.now().astimezone().isoformat(timespec="seconds")}

def read_status(workspace: PaperRawWorkspace)->dict:
    if not workspace.status.exists(): return initial_status(workspace.paper_number)
    value=json.loads(workspace.status.read_text(encoding="utf-8"))
    if value.get("schema_version")!="2.0": return migrate_flat_status(value,workspace.paper_number)
    validate_status(value,workspace.paper_number); return value

def _status_lock_path(workspace: PaperRawWorkspace) -> Path:
    return workspace.root / ".import_status.lock"

def validate_status(value: dict,paper_number: str)->None:
    if value.get("schema_version")!=STATUS_SCHEMA_VERSION or value.get("paper_number")!=paper_number: raise ValueError("invalid nested import status identity")
    for dimension,allowed in ALLOWED.items():
        state=(value.get(dimension) or {}).get("state")
        if state not in allowed: raise ValueError(f"invalid {dimension} state: {state}")

def update_import_status(
    workspace: PaperRawWorkspace,
    *,
    mutator: Callable[[dict], dict],
    timeout: float | None = None,
) -> dict:
    """Atomically read, mutate, validate, and replace nested status v2."""
    lock = FileLock(str(_status_lock_path(workspace)), timeout=-1 if timeout is None else timeout)
    with lock:
        value = read_status(workspace)
        updated = mutator(deepcopy(value))
        if not isinstance(updated, dict):
            raise TypeError("status mutator must return a dict")
        updated["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        validate_status(updated, workspace.paper_number)
        atomic_write_json_unlocked(workspace.status, updated, indent=2)
        return updated

def initialize_status(workspace_root: Path, paper_number: str, value: dict) -> dict:
    """Write a complete status snapshot for a newly built staging workspace."""
    validate_status(value, paper_number)
    root = Path(workspace_root)
    with FileLock(str(root / ".import_status.lock")):
        atomic_write_json_unlocked(root / ".import_status.json", value, indent=2)
    return value

def update_status(workspace: PaperRawWorkspace,dimension: str,state: str,**fields)->dict:
    if dimension not in ALLOWED or state not in ALLOWED[dimension]: raise ValueError(f"invalid status transition target: {dimension}={state}")
    def mutate(value: dict) -> dict:
        value[dimension] = {"state": state, **fields}
        return value
    return update_import_status(workspace, mutator=mutate)

def migrate_flat_status(value: dict,paper_number: str)->dict:
    out=initial_status(paper_number); state=str(value.get("status") or "")
    mapping={"ready_for_convert":("pdf","attached"),"converted":("conversion","complete"),"metadata_matched":("metadata","matched"),"catalog_ready":("catalog","validated"),"ready_for_commit":("formalization","ready"),"imported":("commit","imported"),"committed":("commit","imported"),"formalize_failed":("formalization","failed"),"commit_failed":("commit","failed")}
    if state in mapping:
        dimension,target=mapping[state]; out[dimension]["state"]=target
    return out

def inspect_workspace_readiness(workspace: PaperRawWorkspace)->dict:
    status=read_status(workspace); errors=[]
    try: metadata_freeze=assert_metadata_frozen(workspace.root,workspace.paper_number); metadata_state="frozen"
    except Exception as exc: metadata_freeze=None; metadata_state="missing" if not workspace.metadata.exists() else "invalid"; errors.append(f"metadata: {exc}")
    pdf_state="attached" if workspace.pdf.is_file() else "missing"
    conversion_state="complete" if workspace.conversion.is_file() and workspace.markdown.is_file() and workspace.images.is_dir() else "pending"
    try: catalog_freeze=assert_catalog_frozen(workspace.root,workspace.paper_number); catalog_state="frozen"
    except Exception as exc: catalog_freeze=None; catalog_state="missing" if not workspace.catalog.exists() else "invalid"; errors.append(f"catalog: {exc}")
    formalization_state="ready" if workspace.formalization.is_file() else "pending"
    return {"paper_number":workspace.paper_number,"metadata":metadata_state,"pdf":pdf_state,"conversion":conversion_state,"catalog":catalog_state,"formalization":formalization_state,"ready_for_catalog":metadata_state=="frozen" and conversion_state=="complete","ready_for_formalize":metadata_state=="frozen" and catalog_state=="frozen","ready_for_commit":metadata_state=="frozen" and catalog_state=="frozen" and formalization_state=="ready","errors":errors,"stored_status":status}
