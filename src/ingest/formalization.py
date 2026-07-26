"""Read-only installation planning for a numeric paper_raw workspace."""
from __future__ import annotations

import json
from pathlib import Path

from src.catalog.freeze import assert_catalog_frozen
from src.utils.file_fingerprint import compute_sha256
from src.utils.timestamps import now_iso
from src.ingest.duplicate_inspection import duplicate_inspection_sha256, inspect_ingest_duplicates
from src.ingest.status import inspect_workspace_readiness, update_status
from src.ingest.workspace import PaperRawWorkspace
from src.metadata.freeze import assert_metadata_frozen
from src.utils.atomic_io import atomic_write_json
from src.library.paper_number_ledger import PaperNumberLedger

FORMALIZATION_SCHEMA_VERSION="1.0"

def build_formalization_plan(workspace: PaperRawWorkspace, *, papers_dir: Path, ledger_path: Path|None=None) -> dict:
    ledger_path=ledger_path or papers_dir.parent/"catalog"/"paper_number_ledger.json"
    ledger = PaperNumberLedger(ledger_path)

    # ── Gate: only metadata_staged workspaces can be formalized ──────────
    from src.library.paper_number_ledger import LEDGER_METADATA_STAGED
    from src.workspace.lifecycle import inspect_workspace_lifecycle

    item = (ledger.load().get("items") or {}).get(workspace.paper_number)
    state = str((item or {}).get("state", ""))
    if state != LEDGER_METADATA_STAGED:
        raise ValueError(
            f"workspace_not_metadata_staged: {workspace.paper_number} is "
            f"{state or 'unknown'}, formalize requires metadata_staged"
        )

    inspection = inspect_workspace_lifecycle(workspace.root, ledger_item=item)
    if not inspection.readiness.ready:
        raise ValueError(
            f"workspace_lifecycle_incomplete: {workspace.paper_number}: "
            + "; ".join(inspection.errors)
        )

    metadata_freeze=assert_metadata_frozen(workspace.root,workspace.paper_number)
    catalog_freeze=assert_catalog_frozen(workspace.root,workspace.paper_number,papers_dir=papers_dir,paper_raw_root=workspace.root.parent)
    readiness=inspect_workspace_readiness(workspace)
    if not readiness["ready_for_formalize"]: raise ValueError("workspace is not ready for formalization: "+"; ".join(readiness["errors"]))
    catalog=json.loads(workspace.catalog.read_text(encoding="utf-8")); paper_name=str(catalog["paper_name"]); final_dir=papers_dir/paper_name
    duplicate = inspect_ingest_duplicates(workspace,ledger=ledger,papers_root=papers_dir)
    if duplicate.status != "clear":
        raise ValueError(f"duplicate preflight failed: {duplicate.status}")
    if final_dir.exists(): raise FileExistsError(f"paper_name_conflict: {final_dir}")
    source_assets={
        "metadata":workspace.metadata,"catalog":workspace.catalog,"markdown":workspace.markdown,"pdf":workspace.pdf,
        "metadata_match":workspace.metadata_match,"metadata_freeze":workspace.metadata_freeze,"catalog_task":workspace.catalog_task,
        "catalog_freeze":workspace.catalog_freeze,"conversion_manifest":workspace.conversion,
    }
    missing=[path.name for path in source_assets.values() if not path.is_file()]
    if missing: raise ValueError("formalization source assets missing: "+", ".join(missing))
    image_hashes={p.relative_to(workspace.root).as_posix():compute_sha256(p) for p in sorted(workspace.images.rglob("*")) if p.is_file()}
    source_record_hashes={p.relative_to(workspace.root).as_posix():compute_sha256(p) for p in sorted((workspace.root/"source_records").rglob("*")) if p.is_file()}
    final_filenames={
        "metadata":f"{paper_name}.metadata.json","catalog":f"{paper_name}.catalog.json",
        "markdown":f"{paper_name}.md","pdf":f"{paper_name}.pdf",
        "metadata_match":f"{paper_name}.metadata_match.json",
        "metadata_freeze":f"{paper_name}.metadata_freeze.json",
        "catalog_task":f"{paper_name}.catalog_task.json",
        "catalog_freeze":f"{paper_name}.catalog_freeze.json",
        "conversion_manifest":f"{paper_name}.conversion.json",
        "asset_manifest":f"{paper_name}.asset_manifest.json","marker":workspace.marker.name,
    }
    return {
        "schema_version":FORMALIZATION_SCHEMA_VERSION,"paper_number":workspace.paper_number,"paper_name":paper_name,
        "source_workspace":str(workspace.root.resolve()),"final_directory":str(final_dir.resolve()),
        "source_assets":{kind:{"filename":path.name,"sha256":compute_sha256(path)} for kind,path in source_assets.items()},
        "image_hashes":image_hashes,"source_record_hashes":source_record_hashes,"metadata_freeze_sha256":compute_sha256(workspace.metadata_freeze),"catalog_freeze_sha256":compute_sha256(workspace.catalog_freeze),
        "duplicate_inspection":duplicate.to_dict(),"duplicate_inspection_sha256":duplicate_inspection_sha256(duplicate),
        "final_filenames":final_filenames,"marker_rewrite":{"schema_version":"1.0","paper_number":workspace.paper_number,"folder_name":paper_name,"state":"active","planned_paper_name":paper_name},
        "asset_manifest_plan":{"paper_number":workspace.paper_number,"paper_name":paper_name,"asset_names":final_filenames},
        "created_at":now_iso(),
    }

def write_formalization_plan(workspace: PaperRawWorkspace, *, papers_dir: Path, ledger_path: Path|None=None)->dict:
    before={p.relative_to(workspace.root).as_posix():compute_sha256(p) for p in workspace.root.rglob("*") if p.is_file() and p!=workspace.formalization and p!=workspace.status}
    plan=build_formalization_plan(workspace,papers_dir=papers_dir,ledger_path=ledger_path); atomic_write_json(workspace.formalization,plan,indent=2)
    after={p.relative_to(workspace.root).as_posix():compute_sha256(p) for p in workspace.root.rglob("*") if p.is_file() and p!=workspace.formalization and p!=workspace.status}
    if before!=after: raise RuntimeError("formalize modified a frozen source asset")
    update_status(workspace,"formalization","ready",formalization_sha256=compute_sha256(workspace.formalization),paper_name=plan["paper_name"])
    return plan

def assert_formalization_current(workspace: PaperRawWorkspace, *, papers_dir: Path, ledger_path: Path|None=None)->dict:
    existing=json.loads(workspace.formalization.read_text(encoding="utf-8")); current=build_formalization_plan(workspace,papers_dir=papers_dir,ledger_path=ledger_path)
    for key in ("paper_number","paper_name","source_workspace","final_directory","source_assets","image_hashes","source_record_hashes","metadata_freeze_sha256","catalog_freeze_sha256","duplicate_inspection_sha256","final_filenames","marker_rewrite","asset_manifest_plan"):
        if existing.get(key)!=current.get(key): raise ValueError(f"formalization plan stale: {key}")
    return existing
