"""paper_raw formalization service — the last step before formal commit.

``PaperRawFormalizationService.formalize`` runs entirely inside
``data/paper_raw`` and produces a fully formalized workspace:

  * canonical ``paper_id`` derived from metadata year/first author + catalog naming title
  * folder + asset files renamed from ``<paper_number>`` to ``<paper_id>``
  * 16-digit ``paper_number`` reserved in the ledger at staging (``state=reserved``)
  * ``<paper_id>.catalog.json`` backfilled with library_locator (paper_id / paper_number / asset_refs)
  * ``<16-digit>.paper.number`` marker
  * ``<paper_id>.formalization.json`` manifest
  * ``.import_status.json status = ready_for_commit``

``commit_paper_raw`` then only does final validation + atomic install.
``data/papers`` never receives a half-formalized folder.

Reuse (not reimplementation) of v2_library internals: ``assess_paper_raw_commit_readiness``
(metadata/catalog/duplicate/paper_id gate), ``paper_id_from_metadata_catalog``,
``_backfill_formal_catalog_links``, ``PaperRawConverter.inspect_conversion``.
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from config.settings import (
    ALL_CATALOG_PATH,
    PAPER_NUMBER_LEDGER_PATH,
    PAPER_RAW_DIR,
    PAPERS_DIR,
)
from src.naming import validate_paper_id
from src.path_utils import normalize_repo_path
from src.services.ingest_state import (
    CATALOG_READY,
    FORMALIZE_FAILED,
    POSSIBLE_DUPLICATE,
    READY_FOR_COMMIT,
    read_import_status,
    write_import_status,
)
from src.services.ingest_ids import PAPER_NUMBER_RE
from src.services.source_records import validate_metadata_source_record_exists
from src.services.v2_library import (
    PaperNumberLedger,
    PaperRawConverter,
    assess_paper_raw_commit_readiness,
    backfill_formal_catalog_links,
    load_json_for_gate,
    now_iso,
)


class PaperRawFormalizationService:
    def __init__(
        self,
        *,
        paper_raw_dir: str | Path = PAPER_RAW_DIR,
        papers_dir: str | Path = PAPERS_DIR,
        ledger_path: str | Path = PAPER_NUMBER_LEDGER_PATH,
        all_catalog_path: str | Path = ALL_CATALOG_PATH,
    ):
        self.paper_raw_dir = Path(paper_raw_dir)
        self.papers_dir = Path(papers_dir)
        self.all_catalog_path = Path(all_catalog_path)
        self.ledger = PaperNumberLedger(ledger_path)
        self.converter = PaperRawConverter(paper_raw_dir)

    # -- helpers ----------------------------------------------------------

    def _quarantine_duplicate(self, folder: Path, pid: str, errors: list[str]) -> dict:
        number = self.ledger.paper_number_from_marker(folder)
        qdir = folder.parent / "quarantine" / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{pid}"
        qdir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(folder), qdir)
        from src.utils.atomic_io import atomic_write_json

        write_import_status(
            qdir,
            "quarantined_duplicate",
            reason="possible duplicate detected during formalize",
            errors=errors,
            extra={
                "paper_id": pid,
                "paper_number": number or "",
                "paper_raw_id": number or "",
                "quarantined_duplicate_of": "",
                "duplicate_reasons": errors,
            },
        )
        if number:
            self.ledger.quarantine_reserved_duplicate(
                number,
                qdir,
                duplicate_of="",
                duplicate_reasons=errors,
            )
        atomic_write_json(qdir / "duplicate_report.json", {
            "decision": "possible_duplicate",
            "paper_number": number or "",
            "reasons": errors,
            "created_at": now_iso(),
        }, indent=2)
        return {"success": False, "status": POSSIBLE_DUPLICATE, "quarantine_dir": str(qdir), "errors": errors}

    # -- main entry -------------------------------------------------------

    def formalize(
        self,
        paper_raw_dir: str | Path,
        *,
        paper_id: str | None = None,
        preserve_paper_number: str | None = None,
    ) -> dict:
        folder = Path(paper_raw_dir)
        is_workspace = bool(PAPER_NUMBER_RE.match(folder.name))
        source_id = folder.name
        file_prefix = source_id

        # 1. Conversion gate. Runs on 16-digit paper_number workspaces and
        # already-renamed <paper_id> folders (via inspect_converted_assets,
        # which accepts both). Legacy/untitled dedup is handled by
        # ingest_duplicate_guard, not here.
        # which does not require a 16-digit name). The only skip is a true idempotent rerun:
        # an already-ready_for_commit folder with a formalization.json + marker.
        already_formalized = (
            not is_workspace
            and (folder / f"{folder.name}.formalization.json").exists()
            and self.ledger.paper_number_from_marker(folder) is not None
        )
        if not already_formalized:
            try:
                inspection = self.converter.inspect_converted_assets(folder, file_prefix=file_prefix)
            except Exception as exc:
                write_import_status(folder, FORMALIZE_FAILED, reason=f"conversion inspect failed: {exc}")
                return {"success": False, "status": FORMALIZE_FAILED, "errors": [str(exc)]}
            state = inspection["state"]
            if state != "converted_current":
                error = f"conversion {state}: {inspection['reason']}"
                write_import_status(
                    folder,
                    FORMALIZE_FAILED,
                    reason=error,
                    extra={"conversion_state": state},
                )
                return {
                    "success": False,
                    "status": FORMALIZE_FAILED,
                    "errors": [error],
                    "conversion_state": state,
                }

        # 1b. Idempotency guard: already formalized + status=ready_for_commit -> no-op.
        #     Prevents re-running validation (source records, readiness gate) on a paper
        #     that is already fully formalized, which would overwrite .import_status.json
        #     with formalize_failed if any check fails (e.g. a missing source record file).
        if already_formalized:
            current_status = read_import_status(folder).get("status")
            if current_status == READY_FOR_COMMIT:
                return {
                    "success": True,
                    "status": READY_FOR_COMMIT,
                    "paper_id": folder.name,
                    "paper_number": str(self.ledger.paper_number_from_marker(folder)),
                    "paper_raw_id": source_id,
                }

        # 2. Load metadata + catalog.
        metadata_path = folder / f"{source_id}.metadata.json"
        catalog_path = folder / f"{source_id}.catalog.json"
        metadata, load_errors = load_json_for_gate(metadata_path, "metadata")
        if load_errors:
            write_import_status(folder, FORMALIZE_FAILED, reason="; ".join(load_errors), errors=load_errors)
            return {"success": False, "status": FORMALIZE_FAILED, "errors": load_errors}
        catalog, catalog_errors = load_json_for_gate(catalog_path, "catalog")
        if catalog_errors:
            write_import_status(folder, FORMALIZE_FAILED, reason="; ".join(catalog_errors), errors=catalog_errors)
            return {"success": False, "status": FORMALIZE_FAILED, "errors": catalog_errors}

        # Verify source.raw_record_path points to a real file.
        source = metadata.get("source") or {}
        source_rec_errors = validate_metadata_source_record_exists(
            folder, source.get("raw_record_path", ""),
            require_nonempty=True,
        )
        if source_rec_errors:
            write_import_status(folder, FORMALIZE_FAILED,
                                reason="; ".join(source_rec_errors),
                                errors=source_rec_errors)
            return {"success": False, "status": FORMALIZE_FAILED,
                    "errors": source_rec_errors}

        # 3. Readiness gate (metadata matched/DOI/complete, catalog schema,
        #    Chinese content, duplicate DOI/pdf-sha/md-sha/title-year, paper_id derivation).
        readiness = assess_paper_raw_commit_readiness(
            folder,
            file_prefix=source_id,
            paper_id=paper_id,
            metadata=metadata,
            catalog=catalog,
            papers_dir=self.papers_dir,
            check_duplicates=True,
            require_ready_status=False,
        )
        if not readiness["ready"]:
            errors = readiness["errors"]
            if readiness["status"] == POSSIBLE_DUPLICATE:
                return self._quarantine_duplicate(folder, readiness["paper_id"] or source_id, errors)
            write_import_status(
                folder,
                FORMALIZE_FAILED,
                reason="; ".join(errors),
                errors=errors,
                warnings=readiness["warnings"],
                extra={"readiness_status": readiness["status"], "paper_id": readiness["paper_id"]},
            )
            # Return the specific readiness status (metadata_incomplete /
            # catalog_invalid / paper_id_mismatch / assets_incomplete / not_ready)
            # so callers can branch on the precise failure; .import_status.json
            # is recorded as formalize_failed with readiness_status in extra.
            return {"success": False, "status": readiness["status"], "errors": errors, "readiness_status": readiness["status"]}

        pid = readiness["paper_id"]
        validate_paper_id(pid)
        metadata = readiness["metadata"]
        catalog = readiness["catalog"]

        # 4. Reserve / reuse paper_number (idempotent).
        number = self.ledger.paper_number_from_marker(folder)
        if is_workspace and number is None:
            error = "paper_number workspace is missing reserved .paper.number marker; restage or run migration repair"
            write_import_status(folder, FORMALIZE_FAILED, reason=error, errors=[error])
            return {"success": False, "status": FORMALIZE_FAILED, "errors": [error]}
        if is_workspace and number is not None:
            item = (self.ledger.load().get("items") or {}).get(number) or {}
            state = item.get("state") or "active"
            if state != "reserved":
                error = f"cannot formalize paper_number {number} in ledger state {state}"
                write_import_status(folder, FORMALIZE_FAILED, reason=error, errors=[error])
                return {"success": False, "status": FORMALIZE_FAILED, "errors": [error]}
        if number is None:
            try:
                if preserve_paper_number:
                    number = self.ledger.reserve_specific_for_paper_raw(
                        preserve_paper_number,
                        folder,
                        planned_paper_id=pid,
                    )
                else:
                    number = self.ledger.reserve_for_paper_raw(folder, planned_paper_id=pid)
            except Exception as exc:
                errors = [str(exc)]
                write_import_status(folder, FORMALIZE_FAILED, reason="; ".join(errors), errors=errors)
                return {"success": False, "status": FORMALIZE_FAILED, "errors": errors}

        # 5. Rename folder + asset files <paper_number>.* -> <pid>.*.
        target = folder
        if is_workspace:
            target = folder.with_name(pid)
            if target.exists() and target.resolve() != folder.resolve():
                errors = [f"paper_id target already exists: {pid}"]
                write_import_status(folder, FORMALIZE_FAILED, reason="; ".join(errors), errors=errors)
                return {"success": False, "status": FORMALIZE_FAILED, "errors": errors}
            folder.rename(target)
            for suffix_name in ("metadata.json", "catalog.json", "md", "pdf"):
                old = target / f"{source_id}.{suffix_name}"
                new = target / f"{pid}.{suffix_name}"
                if old.exists() and old != new:
                    old.rename(new)
            repoint_reserved = getattr(self.ledger, "repoint_reserved")
            repoint_reserved(number, target, planned_paper_id=pid)

        # 6. Backfill catalog links in paper_raw.
        backfill_formal_catalog_links(target, pid, number)
        # asset hashes live in <paper_id>.asset_manifest.json; write the
        # possibly DOI-normalized citation metadata back.
        from src.utils.atomic_io import atomic_write_json

        atomic_write_json(target / f"{pid}.metadata.json", metadata, indent=2)

        # 7. formalization.json manifest.
        atomic_write_json(target / f"{pid}.formalization.json", {
            "paper_id": pid,
            "paper_number": number,
            "paper_raw_id": readiness["paper_raw_id"],
            "pdf_sha256": readiness["pdf_sha256"],
            "markdown_sha256": readiness["markdown_sha256"],
            "ledger_state": "reserved",
            "warnings": readiness["warnings"],
            "formalized_at": now_iso(),
        }, indent=2)

        # 8. ready_for_commit.
        write_import_status(
            target,
            READY_FOR_COMMIT,
            reason="formalized: renamed, paper_number reserved, catalog backfilled",
            warnings=readiness["warnings"],
            extra={
                "paper_id": pid,
                "paper_number": number,
                "paper_raw_id": readiness["paper_raw_id"],
                "pdf_sha256": readiness["pdf_sha256"],
                "markdown_sha256": readiness["markdown_sha256"],
            },
        )
        return {
            "success": True,
            "status": READY_FOR_COMMIT,
            "paper_id": pid,
            "paper_number": number,
            "folder": str(target),
            "paper_raw_id": readiness["paper_raw_id"],
        }
