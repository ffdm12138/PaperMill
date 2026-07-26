"""Active numeric paper_raw allocation and MinerU conversion services."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from filelock import FileLock
from config.settings import (
    MINERU_BACKEND, MINERU_EFFORT, MINERU_LANG, MINERU_METHOD,
    MINERU_OUTPUT_CACHE_DIR, MINERU_OUTPUT_CACHE_ENABLED,
    PAPER_NUMBER_LEDGER_PATH, PAPER_RAW_DIR, PAPERS_DIR,
)
from src.ingest.locking import paper_raw_write_lock
from src.cleaner import MinerUOutputCleaner
from src.converter import MinerUConverter
from src.file_fingerprint import compute_file_hashes, compute_sha256
from src.ingest.models import now_iso
from src.library.paper_number_ledger import PaperNumberLedger
from src.metadata.schema import METADATA_SCHEMA_VERSION, empty_metadata, metadata_doi, validate_metadata_schema
from src.metadata.freeze import assert_metadata_frozen
from src.naming import safe_child
from src.path_utils import normalize_repo_path
from src.services.asset_manifest import write_asset_manifest
from src.services.ingest_duplicate_guard import (
    DuplicateIngestError,
    check_pdf_duplicate,
)
from src.utils.jsonio import read_json_strict
from src.utils.identifiers import PAPER_NUMBER_RE, validate_paper_raw_id
from src.services.ingest_state import METADATA_MANUAL_REVIEW_REQUIRED, STAGE_FAILED, write_import_status as _write_import_status
from src.services.mineru_output_cache import MinerUOutputCache
from src.services.source_records import ensure_raw_record_path_is_metadata_source, manual_metadata_source_record, metadata_source_rel_path, write_metadata_source_record
from src.services.stage_manifest import doi_fetch_pdf_source, manual_pdf_source, read_stage_manifest, update_stage_manifest, write_stage_manifest
from src.utils.fs import replace_images_dir
from src.utils.atomic_io import atomic_write_json, atomic_write_text

_PAPER_NUMBER_RE = PAPER_NUMBER_RE

def _read_json(path: Path, default: dict | None = None) -> dict:
    if not path.exists():
        return default or {}
    return read_json_strict(path)

def _write_text_atomic(path: Path, text: str) -> None:
    atomic_write_text(path, text, fsync=False)

class PaperRawAllocator:
    def __init__(
        self,
        paper_raw_dir: str | Path = PAPER_RAW_DIR,
        *,
        ledger_path: str | Path = PAPER_NUMBER_LEDGER_PATH,
        papers_dir: str | Path = PAPERS_DIR,
    ):
        self.paper_raw_dir = Path(paper_raw_dir)
        self.ledger = PaperNumberLedger(ledger_path)
        self.papers_dir = Path(papers_dir)

    @property
    def _lock_path(self) -> Path:
        return self.paper_raw_dir / ".paper_raw_write.lock"

    def allocate_id(self) -> str:
        raise RuntimeError("legacy short-id allocation is legacy only; use allocate_workspace()")

    def allocate_workspace(self, *, planned_paper_name: str = "") -> dict:
        """Reserve a 16-digit paper_number and create its paper_raw workspace."""
        self.paper_raw_dir.mkdir(parents=True, exist_ok=True)
        with paper_raw_write_lock(self.paper_raw_dir):
            return self._allocate_workspace_unlocked(planned_paper_name=planned_paper_name)

    def _allocate_workspace_unlocked(self, *, planned_paper_name: str = "") -> dict:
        number, folder = self.ledger.reserve_next_for_paper_raw_workspace(
            self.paper_raw_dir,
            planned_paper_name=planned_paper_name,
        )
        return {
            "paper_number": number,
            "paper_raw_id": number,
            "folder": str(folder),
        }

    def _workspace_has_core_assets(self, folder: Path, paper_number: str) -> bool:
        if not folder.exists():
            return False
        patterns = (
            f"{paper_number}.metadata.json",
            f"{paper_number}.pdf",
            "stage_manifest.json",
            f"{paper_number}.asset_manifest.json",
            "source_records/*.json",
            "*.paper.number",
        )
        return any(any(folder.glob(pattern)) for pattern in patterns)

    def _mark_allocation_failure(self, paper_number: str, folder: Path, exc: Exception) -> None:
        if self._workspace_has_core_assets(folder, paper_number):
            self._mark_stage_failed(paper_number, folder, exc)
            return
        try:
            self.ledger.mark_abandoned(paper_number, str(exc), folder=folder)
        except Exception:
            pass

    def _mark_stage_failed(self, paper_number: str, folder: Path, exc: Exception) -> None:
        """Record staging failure in .import_status.json only.

        The ledger stays at ``reserved`` — staging failure is an operation
        result, not a lifecycle state. The workspace remains unsettled so the
        index re-scans it on the next drain.
        """
        try:
            _write_import_status(
                folder,
                STAGE_FAILED,
                reason=str(exc),
                errors=[str(exc)],
                extra={"paper_number": paper_number, "paper_raw_id": paper_number},
            )
        except Exception:
            pass

    def allocate_from_pdf(
        self,
        source_pdf: str | Path,
        *,
        source_type: str = "manual_pdf",
        metadata: dict | None = None,
        move: bool = False,
    ) -> dict:
        source_pdf = Path(source_pdf)
        if not source_pdf.exists():
            raise FileNotFoundError(f"PDF not found: {source_pdf}")
        self.paper_raw_dir.mkdir(parents=True, exist_ok=True)
        with paper_raw_write_lock(self.paper_raw_dir):
            dup = check_pdf_duplicate(source_pdf, paper_raw_dir=self.paper_raw_dir, papers_dir=self.papers_dir)
            if dup.blocking:
                raise DuplicateIngestError(dup)
            original_hashes = compute_file_hashes(source_pdf)
            workspace = self._allocate_workspace_unlocked()
            source_id = workspace["paper_number"]
            folder = Path(workspace["folder"])
            try:
                dest_pdf = folder / f"{source_id}.pdf"
                if move:
                    shutil.move(str(source_pdf), dest_pdf)
                else:
                    shutil.copy2(source_pdf, dest_pdf)
                data = metadata or empty_metadata(source_id, source_type=source_type)
                data["paper_number"] = source_id
                data["paper_raw_id"] = source_id
                data["schema_version"] = METADATA_SCHEMA_VERSION
                data["source_type"] = source_type
                source_obj = data.setdefault("source", {})
                source_obj["kind"] = source_type
                if source_type == "manual_pdf":
                    source_obj["provider"] = "manual"
                    source_obj["raw_record_path"] = metadata_source_rel_path("manual")
                else:
                    if not source_obj.get("provider"):
                        source_obj["provider"] = source_type
                    source_obj["raw_record_path"] = ensure_raw_record_path_is_metadata_source(
                        source_obj.get("raw_record_path") or "",
                        source_obj.get("provider") or source_type,
                    )
                schema_errors = validate_metadata_schema(data)

                if schema_errors:
                    raise ValueError("invalid metadata: " + "; ".join(schema_errors))
                staged_hashes = compute_file_hashes(dest_pdf)
                operation = "move" if move else "copy"
                atomic_write_json(folder / f"{source_id}.metadata.json", data, indent=2)
                write_metadata_source_record(folder, "manual", manual_metadata_source_record(
                    original_filename=source_pdf.name,
                    original_path=str(source_pdf),
                    note="metadata unresolved at staging time",
                ))
                write_asset_manifest(folder, prefix=source_id, paper_number=source_id, stage="paper_raw")
                write_stage_manifest(
                    folder,
                    paper_number=source_id,
                    paper_raw_id=source_id,
                    workflow_path="manual_pdf",
                    source_type=source_type,
                    pdf_source=manual_pdf_source(
                        operation=operation,
                        original_path=str(source_pdf),
                        original_filename=source_pdf.name,
                        original_hashes=original_hashes,
                    ),
                    staged_pdf={
                        "path": normalize_repo_path(dest_pdf),
                        "md5": staged_hashes["md5"],
                        "sha256": staged_hashes["sha256"],
                        "file_size": staged_hashes["file_size"],
                    },
                )
                _write_import_status(
                    folder,
                    "ready_for_convert",
                    reason="PDF staged into paper_raw workspace",
                    extra={
                        "paper_number": source_id,
                        "paper_raw_id": source_id,
                        "source_type": source_type,
                        "source_provider": "manual_pdf",
                        "doi": "",
                        "pdf_md5": staged_hashes["md5"],
                        "pdf_sha256": staged_hashes["sha256"],
                    },
                )
                self.ledger.mark_metadata_staged(source_id, folder)
                return {**workspace, "pdf": str(dest_pdf)}
            except Exception as exc:
                self._mark_allocation_failure(source_id, folder, exc)
                raise

    def attach_pdf(self, source_id: str, source_pdf: str | Path, *, move: bool = False, replace: bool = False) -> dict:
        paper_number = validate_paper_raw_id(source_id)
        folder = safe_child(self.paper_raw_dir, paper_number)
        if not folder.is_dir():
            raise FileNotFoundError(f"paper_raw folder not found: {folder}")
        source_pdf = Path(source_pdf)
        with paper_raw_write_lock(self.paper_raw_dir):
            dest_pdf = folder / f"{paper_number}.pdf"
            freeze_path = folder / f"{paper_number}.metadata_freeze.json"
            if replace and freeze_path.exists():
                # A frozen receipt closes over the PDF hash.  Normal ingest
                # may not invalidate that closure; the admin revision flow is
                # the only supported way to replace frozen inputs.
                assert_metadata_frozen(folder, paper_number)
                raise PermissionError("cannot replace PDF after metadata freeze")
            dup = check_pdf_duplicate(
                source_pdf,
                paper_raw_dir=self.paper_raw_dir,
                papers_dir=self.papers_dir,
                skip_paper_number=paper_number,
            )
            if dup.blocking:
                raise DuplicateIngestError(dup)
            backup_pdf = dest_pdf.with_suffix(dest_pdf.suffix + ".replace.tmp")
            if dest_pdf.exists():
                if not replace:
                    source_hashes = compute_file_hashes(source_pdf)
                    existing_hashes = compute_file_hashes(dest_pdf)
                    if (
                        source_hashes["md5"] == existing_hashes["md5"]
                        and source_hashes["sha256"] == existing_hashes["sha256"]
                        and source_hashes["file_size"] == existing_hashes["file_size"]
                    ):
                        return {
                            "success": True,
                            "skipped": True,
                            "status": "skipped_existing_pdf",
                            "paper_number": paper_number,
                            "paper_raw_id": paper_number,
                            "pdf": str(dest_pdf),
                            "pdf_md5": existing_hashes["md5"],
                            "pdf_sha256": existing_hashes["sha256"],
                            "pdf_file_size": existing_hashes["file_size"],
                            "reason": "same PDF already attached",
                        }
                    raise FileExistsError(f"PDF already exists; pass replace=True to overwrite: {dest_pdf}")
                if backup_pdf.exists():
                    backup_pdf.unlink()
                dest_pdf.replace(backup_pdf)
            try:
                if move:
                    shutil.move(str(source_pdf), dest_pdf)
                else:
                    shutil.copy2(source_pdf, dest_pdf)
            except Exception:
                if backup_pdf.exists() and not dest_pdf.exists():
                    backup_pdf.replace(dest_pdf)
                raise
            else:
                backup_pdf.unlink(missing_ok=True)
            hashes = compute_file_hashes(dest_pdf)
            write_asset_manifest(folder, prefix=paper_number, paper_number=paper_number, stage="paper_raw")
            existing_manifest = read_stage_manifest(folder)
            workflow_path = existing_manifest.get("workflow_path") or "network_metadata_pdf_fetch"
            if workflow_path == "network_metadata":
                workflow_path = "network_metadata_pdf_fetch"
            meta_path = folder / f"{paper_number}.metadata.json"
            data = _read_json(meta_path, {})
            source_obj = data.get("source") if isinstance(data.get("source"), dict) else {}
            source_type = str(existing_manifest.get("source_type") or data.get("source_type") or "manual_pdf")
            existing_pdf_source = existing_manifest.get("pdf_source") if isinstance(existing_manifest.get("pdf_source"), dict) else None
            if existing_pdf_source is None:
                existing_pdf_source = doi_fetch_pdf_source(operation="replace" if replace else "attach")
            else:
                existing_pdf_source["operation"] = "replace" if replace else "attach"
            update_stage_manifest(folder, updates={
                "schema_version": "1.0",
                "paper_number": paper_number,
                "paper_raw_id": paper_number,
                "workflow_path": workflow_path,
                "source_type": source_type,
                "pdf_source": existing_pdf_source,
                "staged_pdf": {
                    "path": normalize_repo_path(dest_pdf),
                    "md5": hashes["md5"],
                    "sha256": hashes["sha256"],
                    "file_size": hashes["file_size"],
                },
                "last_pdf_operation": "replace" if replace else "attach",
                "pdf_attached_at": now_iso(),
            })
            _write_import_status(
                folder,
                "ready_for_convert",
                reason="PDF attached into paper_raw workspace",
                extra={
                    "paper_number": paper_number,
                    "paper_raw_id": paper_number,
                    "source_type": source_type,
                    "source_provider": (source_obj or {}).get("provider") or source_type,
                    "doi": metadata_doi(data),
                    "pdf_md5": hashes["md5"],
                    "pdf_sha256": hashes["sha256"],
                },
            )
            return {
                "paper_number": paper_number,
                "paper_raw_id": paper_number,
                "pdf": str(dest_pdf),
                "pdf_md5": hashes["md5"],
                "pdf_sha256": hashes["sha256"],
                "pdf_file_size": hashes["file_size"],
            }

class PaperRawConverter:
    def __init__(
        self,
        paper_raw_dir: str | Path = PAPER_RAW_DIR,
        converter: MinerUConverter | None = None,
        cleaner: MinerUOutputCleaner | None = None,
        output_cache: MinerUOutputCache | None = None,
        reuse_output_cache: bool | None = None,
    ):
        self.paper_raw_dir = Path(paper_raw_dir)
        self.converter = converter or MinerUConverter()
        self.cleaner = cleaner or MinerUOutputCleaner()
        self.output_cache = output_cache or MinerUOutputCache(MINERU_OUTPUT_CACHE_DIR, cleaner=self.cleaner)
        self.reuse_output_cache = MINERU_OUTPUT_CACHE_ENABLED if reuse_output_cache is None else reuse_output_cache

    def _source_folder(self, source_id_or_dir: str | Path) -> tuple[str, Path]:
        value = Path(source_id_or_dir)
        if value.is_dir():
            folder = value
            workspace_id = folder.name
        else:
            workspace_id = str(source_id_or_dir)
            folder = safe_child(self.paper_raw_dir, workspace_id)
        validate_paper_raw_id(workspace_id)
        try:
            folder.resolve().relative_to(self.paper_raw_dir.resolve())
        except ValueError:
            raise ValueError(f"MinerU v2 input outside paper_raw: {folder}")
        return workspace_id, folder

    def _conversion_paths(self, folder: Path, source_id: str) -> dict[str, Path]:
        return {
            "pdf": folder / f"{source_id}.pdf",
            "markdown": folder / f"{source_id}.md",
            "images": folder / "images",
            "manifest": folder / f"{source_id}.conversion.json",
            "output": folder / "output",
        }

    def _images_count(self, images_dir: Path) -> int:
        if not images_dir.exists() or not images_dir.is_dir():
            return 0
        return sum(1 for p in images_dir.rglob("*") if p.is_file())

    def inspect_conversion(
        self,
        source_id_or_dir: str | Path,
        *,
        backend: str = MINERU_BACKEND,
        method: str = MINERU_METHOD,
        lang: str = MINERU_LANG,
        effort: str = MINERU_EFFORT,
    ) -> dict:
        source_id, folder = self._source_folder(source_id_or_dir)
        return self.inspect_converted_assets(
            folder, file_prefix=source_id, backend=backend, method=method, lang=lang, effort=effort
        )

    def inspect_output_cache(
        self,
        source_id_or_dir: str | Path,
        *,
        backend: str = MINERU_BACKEND,
        method: str = MINERU_METHOD,
        lang: str = MINERU_LANG,
        effort: str = MINERU_EFFORT,
        force_reconvert: bool = False,
        reuse_output_cache: bool | None = None,
    ) -> dict:
        enabled = self.reuse_output_cache if reuse_output_cache is None else reuse_output_cache
        source_id, folder = self._source_folder(source_id_or_dir)
        pdf = folder / f"{source_id}.pdf"
        if force_reconvert:
            return {
                "hit": False,
                "output_cache_enabled": bool(enabled),
                "output_cache_state": "bypassed",
                "output_cache_reason": "--force-reconvert requested",
                "output_cache_dir": "",
                "output_cache_manifest": "",
            }
        if not enabled:
            return {
                "hit": False,
                "output_cache_enabled": False,
                "output_cache_state": "disabled",
                "output_cache_reason": "output cache disabled",
                "output_cache_dir": "",
                "output_cache_manifest": "",
            }
        if not pdf.exists():
            return {
                "hit": False,
                "output_cache_enabled": True,
                "output_cache_state": "miss",
                "output_cache_reason": "missing paper_raw PDF",
                "output_cache_dir": "",
                "output_cache_manifest": "",
            }
        hit = self.output_cache.find(
            pdf,
            backend=backend,
            method=method,
            lang=lang,
            effort=effort,
            stem=source_id,
        )
        return {
            "hit": hit.ok,
            "output_cache_enabled": True,
            "output_cache_state": hit.state,
            "output_cache_reason": hit.reason,
            "output_cache_dir": normalize_repo_path(hit.cache_dir or ""),
            "output_cache_manifest": normalize_repo_path(hit.manifest_path or ""),
        }

    def inspect_converted_assets(
        self,
        folder: str | Path,
        *,
        file_prefix: str,
        backend: str = MINERU_BACKEND,
        method: str = MINERU_METHOD,
        lang: str = MINERU_LANG,
        effort: str = MINERU_EFFORT,
    ) -> dict:
        """Classify the conversion state of a folder by ``file_prefix``.

        Unlike ``inspect_conversion``, this does NOT require a paper_number
        folder name — it inspects ``<file_prefix>.conversion.json`` /
        ``<file_prefix>.md`` / ``images/`` directly, so it works on an already
        formalized ``<paper_name>`` workspace as well as a 6-digit source folder.
        """
        folder = Path(folder)
        pdf = folder / f"{file_prefix}.pdf"
        target_md = folder / f"{file_prefix}.md"
        images_target = folder / "images"
        manifest_path = folder / f"{file_prefix}.conversion.json"
        pdf_sha = compute_sha256(pdf) if pdf.exists() else ""
        md_exists = target_md.exists() and target_md.stat().st_size > 0
        images_exists = images_target.exists() and images_target.is_dir()
        result = {
            "state": "not_converted",
            "reason": "no converted Markdown/assets present",
            "manifest": None,
            "markdown": str(target_md),
            "images_dir": str(images_target),
            "pdf_sha256": pdf_sha,
        }


        if manifest_path.exists():
            try:
                manifest = _read_json(manifest_path, {})
            except Exception as exc:
                result.update({
                    "state": "partial",
                    "reason": f"conversion manifest is unreadable: {exc}",
                })
                return result
            result["manifest"] = manifest
            missing: list[str] = []
            if manifest.get("status") != "converted":
                missing.append("manifest status is not converted")
            if not md_exists:
                missing.append("markdown missing or empty")
            if not images_exists:
                missing.append("images directory missing")
            if missing:
                result.update({"state": "partial", "reason": "; ".join(missing)})
                return result
            stale: list[str] = []
            if str(manifest.get("pdf_sha256") or "") != pdf_sha:
                stale.append("PDF sha256 changed")
            for key, current in {
                "backend": backend,
                "method": method,
                "lang": lang,
                "effort": effort,
            }.items():
                if str(manifest.get(key) or "") != str(current):
                    stale.append(f"{key} changed")
            if stale:
                result.update({"state": "stale", "reason": "; ".join(stale)})
                return result
            result.update({"state": "converted_current", "reason": "conversion manifest is current"})
            return result

        if md_exists and images_exists:
            result.update({
                "state": "conversion_manifest_missing",
                "reason": "markdown/images exist but conversion manifest is missing",
            })
            return result
        if target_md.exists() or images_target.exists():
            result.update({
                "state": "partial",
                "reason": "partial converted assets present without conversion manifest",
            })
            return result
        return result

    def _clear_conversion_outputs(self, folder: Path, source_id: str) -> None:
        paths = self._conversion_paths(folder, source_id)
        for file_path in (paths["markdown"], paths["manifest"]):
            file_path.unlink(missing_ok=True)
        for dir_path in (paths["images"], paths["output"]):
            if dir_path.exists():
                shutil.rmtree(dir_path)

    def _replace_images_dir(self, images_source: Path | None, images_target: Path) -> int:
        return replace_images_dir(images_source, images_target)

    def convert(
        self,
        source_id_or_dir: str | Path,
        *,
        output_root: str | Path | None = None,
        force_reconvert: bool = False,
        skip_existing: bool = True,
        cache_only: bool = False,
    ) -> dict:
        source_id, folder = self._source_folder(source_id_or_dir)
        pdf = folder / f"{source_id}.pdf"
        meta = folder / f"{source_id}.metadata.json"
        if not pdf.exists() or not meta.exists():
            raise FileNotFoundError(f"paper_raw source requires {source_id}.pdf and {source_id}.metadata.json")
        metadata = _read_json(meta)
        schema_errors = validate_metadata_schema(metadata)
        if schema_errors:
            raise ValueError("; ".join(schema_errors))
        inspection = self.inspect_conversion(
            folder,
            backend=MINERU_BACKEND,
            method=MINERU_METHOD,
            lang=MINERU_LANG,
            effort=MINERU_EFFORT,
        )
        state = inspection["state"]
        if skip_existing and not force_reconvert and state == "converted_current":
            return {
                "success": True,
                "skipped": True,
                "status": "skipped_existing",
                "paper_number": source_id,
                "paper_raw_id": source_id,
                "reason": inspection["reason"],
                "conversion_state": state,
                "markdown": inspection["markdown"],
                "images_dir": inspection["images_dir"],
            }
        cache_hit = None
        if not force_reconvert and self.reuse_output_cache:
            cache_hit = self.output_cache.find(
                pdf,
                backend=MINERU_BACKEND,
                method=MINERU_METHOD,
                lang=MINERU_LANG,
                effort=MINERU_EFFORT,
                stem=source_id,
            )
            if cache_hit.ok:
                restored = self.output_cache.restore_to_paper_raw(
                    hit=cache_hit,
                    folder=folder,
                    paper_raw_id=source_id,
                    backend=MINERU_BACKEND,
                    method=MINERU_METHOD,
                    lang=MINERU_LANG,
                    effort=MINERU_EFFORT,
                    output_cache_enabled=self.reuse_output_cache,
                )
                return {
                    "success": True,
                    "paper_number": source_id,
                    "paper_raw_id": source_id,
                    "markdown": restored["markdown"],
                    "images_dir": restored["images_dir"],
                    "output_dir": normalize_repo_path(cache_hit.output_dir or cache_hit.cache_dir or ""),
                    "conversion_manifest": restored["conversion_manifest"],
                    "conversion_state": "converted_current",
                    "restored_from_output_cache": True,
                    "cache_hit": True,
                    "cache_reason": cache_hit.reason,
                    "output_cache_state": cache_hit.state,
                    "output_cache_dir": normalize_repo_path(cache_hit.cache_dir or ""),
                    "output_cache_manifest": normalize_repo_path(cache_hit.manifest_path or ""),
                }
        if cache_only:
            reason = cache_hit.reason if cache_hit else "output cache disabled"

            state_name = cache_hit.state if cache_hit else "disabled"
            return {
                "success": False,
                "status": "cache_miss",
                "paper_number": source_id,
                "paper_raw_id": source_id,
                "conversion_state": state,
                "error": reason,
                "cache_hit": False,
                "output_cache_state": state_name,
                "output_cache_reason": reason,
            }
        if not force_reconvert and state in {"stale", "partial"}:
            status = "stale_conversion" if state == "stale" else "partial_conversion"
            return {
                "success": False,
                "status": status,
                "paper_number": source_id,
                "paper_raw_id": source_id,
                "conversion_state": state,
                "error": f"{inspection['reason']}; pass --force-reconvert to rebuild",
            }
        if force_reconvert:
            self._clear_conversion_outputs(folder, source_id)
        output_root = Path(output_root) if output_root else folder / "output"
        conv = self.converter.convert(
            pdf,
            output_root,
            backend=MINERU_BACKEND,
            method=MINERU_METHOD,
            lang=MINERU_LANG,
            effort=MINERU_EFFORT,
            paper_name=source_id,
        )
        if not conv.get("success"):
            return {**conv, "paper_number": source_id, "paper_raw_id": source_id}
        source_dir = Path(conv["output_dir"])
        md_path = self.cleaner.locate_markdown(
            source_dir,
            method=MINERU_METHOD,
            stem=pdf.stem,
            backend=MINERU_BACKEND,
        )
        if md_path is None:
            return {"success": False, "paper_number": source_id, "paper_raw_id": source_id, "error": "MinerU output markdown not found"}
        text = md_path.read_text(encoding="utf-8").replace("](./images/", "](images/")
        target_md = folder / f"{source_id}.md"
        _write_text_atomic(target_md, text)
        images_target = folder / "images"
        images_source = self.cleaner.locate_images_dir(source_dir, md_path)
        images_count = self._replace_images_dir(images_source, images_target)
        pdf_hashes = compute_file_hashes(pdf)
        pdf_sha = pdf_hashes["sha256"]
        markdown_sha = compute_sha256(target_md)
        try:
            from src.mineru_runtime import runtime_config_from_env

            runtime_cfg = runtime_config_from_env()
            runner = runtime_cfg.runner.value
            api_url = runtime_cfg.api_url
        except Exception:
            runner = ""
            api_url = ""
        cache_registration: dict[str, Any] = {}
        if self.reuse_output_cache:
            try:
                cache_registration = self.output_cache.register(
                    source_output_dir=source_dir,
                    pdf_path=pdf,
                    source_paper_raw_id=source_id,
                    backend=MINERU_BACKEND,
                    method=MINERU_METHOD,
                    lang=MINERU_LANG,
                    effort=MINERU_EFFORT,
                    runner=runner,
                    api_url=api_url,
                )
            except Exception as exc:
                cache_registration = {"registered": False, "error": str(exc)}
        manifest = {
            "schema_version": "1.0",
            "status": "converted",
            "paper_number": source_id,
            "paper_raw_id": source_id,
            "pdf_md5": pdf_hashes["md5"],
            "pdf_sha256": pdf_sha,
            "pdf_file_size": pdf_hashes["file_size"],
            "markdown_path": f"{source_id}.md",
            "markdown_sha256": markdown_sha,
            "images_dir": "images",
            "images_count": images_count,
            "backend": MINERU_BACKEND,
            "method": MINERU_METHOD,
            "lang": MINERU_LANG,
            "effort": MINERU_EFFORT,
            "runner": runner,
            "api_url": api_url,
            "output_dir": normalize_repo_path(source_dir),
            "conversion_source": "mineru",
            "restored_from_output_cache": False,
            "output_cache_enabled": self.reuse_output_cache,
            "output_cache_hit": False,
            "output_cache_dir": cache_registration.get("cache_dir", ""),
            "output_cache_manifest": cache_registration.get("manifest_path", ""),
            "converted_at": now_iso(),
        }
        if cache_registration.get("error"):
            manifest["output_cache_error"] = cache_registration["error"]
        atomic_write_json(folder / f"{source_id}.conversion.json", manifest, indent=2)
        _write_import_status(
            folder,
            "converted",
            reason="MinerU conversion completed",
            extra={
                "paper_number": source_id,
                "paper_raw_id": source_id,
                "pdf_md5": pdf_hashes["md5"],
                "pdf_sha256": pdf_sha,
                "markdown_sha256": markdown_sha,
                "output_cache_dir": cache_registration.get("cache_dir", ""),
            },
        )
        write_asset_manifest(folder, prefix=source_id, paper_number=source_id, stage="paper_raw")
        return {
            "success": True,
            "paper_number": source_id,
            "paper_raw_id": source_id,
            "markdown": str(target_md),
            "images_dir": str(images_target),
            "output_dir": str(source_dir),
            "conversion_manifest": str(folder / f"{source_id}.conversion.json"),
            "conversion_state": "converted_current",
            "restored_from_output_cache": False,
            "cache_hit": False,
            "output_cache_dir": cache_registration.get("cache_dir", ""),
            "output_cache_manifest": cache_registration.get("manifest_path", ""),
        }
