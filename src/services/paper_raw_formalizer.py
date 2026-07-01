"""paper_raw formalization service — the last step before formal commit.

``PaperRawFormalizationService.formalize`` runs entirely inside
``data/paper_raw`` and produces a fully formalized workspace:

  * canonical ``paper_id`` derived from metadata (year + first author + short_zh)
  * folder + asset files renamed from ``<source_id>`` to ``<paper_id>``
  * 16-digit ``paper_number`` reserved in the ledger (``state=reserved``)
  * ``<paper_id>.catalog.json`` backfilled with paper_id / paper_number / asset_refs
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
    write_import_status,
)
from src.services.v2_library import (
    PaperNumberLedger,
    PaperRawConverter,
    _TEMP_ID_RE,
    _backfill_formal_catalog_links,
    _load_json_for_gate,
    assess_paper_raw_commit_readiness,
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

    @staticmethod
    def _quarantine_duplicate(folder: Path, pid: str, errors: list[str]) -> dict:
        qdir = folder.parent / "quarantine" / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{pid}"
        qdir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(folder), qdir)
        from src.utils.atomic_io import atomic_write_json

        atomic_write_json(qdir / "duplicate_report.json", {
            "decision": "possible_duplicate",
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
        is_temp = bool(_TEMP_ID_RE.match(folder.name))
        source_id = folder.name

        # 1. Conversion gate — MUST run while the folder is still 6-digit
        #    (PaperRawConverter.inspect_conversion only accepts <000001>).
        if is_temp:
            try:
                inspection = self.converter.inspect_conversion(folder)
            except Exception as exc:
                write_import_status(folder, FORMALIZE_FAILED, reason=f"conversion inspect failed: {exc}")
                return {"success": False, "status": FORMALIZE_FAILED, "errors": [str(exc)]}
            state = inspection["state"]
            if state not in {"converted_current", "converted_legacy"}:
                write_import_status(
                    folder,
                    FORMALIZE_FAILED,
                    reason=f"conversion not current: {state} ({inspection['reason']})",
                    extra={"conversion_state": state},
                )
                return {
                    "success": False,
                    "status": FORMALIZE_FAILED,
                    "errors": [f"conversion {state}: {inspection['reason']}"],
                    "conversion_state": state,
                }

        # 2. Load metadata + catalog.
        metadata_path = folder / f"{source_id}.metadata.json"
        catalog_path = folder / f"{source_id}.catalog.json"
        metadata, load_errors = _load_json_for_gate(metadata_path, "metadata")
        if load_errors:
            write_import_status(folder, FORMALIZE_FAILED, reason="; ".join(load_errors), errors=load_errors)
            return {"success": False, "status": FORMALIZE_FAILED, "errors": load_errors}
        catalog, catalog_errors = _load_json_for_gate(catalog_path, "catalog")
        if catalog_errors:
            write_import_status(folder, FORMALIZE_FAILED, reason="; ".join(catalog_errors), errors=catalog_errors)
            return {"success": False, "status": FORMALIZE_FAILED, "errors": catalog_errors}

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
            return {"success": False, "status": FORMALIZE_FAILED, "errors": errors, "readiness_status": readiness["status"]}

        pid = readiness["paper_id"]
        validate_paper_id(pid)
        metadata = readiness["metadata"]
        catalog = readiness["catalog"]

        # 4. Reserve / reuse paper_number (idempotent).
        number = self.ledger.paper_number_from_marker(folder)
        if number is None:
            if preserve_paper_number:
                number = self.ledger.repoint(preserve_paper_number, folder)
            else:
                number = self.ledger.reserve_for_paper_raw(folder, planned_paper_id=pid)

        # 5. Rename folder + asset files <source_id>.* -> <pid>.* (only if still temp).
        target = folder
        if is_temp:
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
            # ensure marker carries the renamed folder name
            marker = target / f"{number}.paper.number"
            if marker.exists():
                from src.utils.atomic_io import atomic_write_json

                atomic_write_json(marker, {
                    "paper_number": number,
                    "folder_name": target.name,
                    "state": "reserved",
                    "planned_paper_id": pid,
                }, indent=2)

        # 6. Backfill catalog links in paper_raw.
        _backfill_formal_catalog_links(target, pid, number)
        # metadata.pdf.path / content.markdown_sha256 already set by readiness;
        # write the (possibly DOI-normalized) metadata back.
        from src.utils.atomic_io import atomic_write_json

        atomic_write_json(target / f"{pid}.metadata.json", metadata, indent=2)

        # 7. formalization.json manifest.
        atomic_write_json(target / f"{pid}.formalization.json", {
            "paper_id": pid,
            "paper_number": number,
            "source_id": readiness["source_id"],
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
                "source_id": readiness["source_id"],
                "paper_id": pid,
                "paper_number": number,
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
            "source_id": readiness["source_id"],
        }
