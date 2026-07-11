"""Permanent paper-number ledger and marker lifecycle helpers."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from filelock import FileLock

from config.settings import PAPER_NUMBER_LEDGER_PATH, PAPERS_DIR
from src.path_utils import normalize_repo_path, resolve_stored_path
from src.naming import safe_child
from src.services.ingest_ids import PAPER_NUMBER_RE
from src.services.ingest_state import STAGE_FAILED
from src.utils.atomic_io import atomic_write_json
from src.ingest.marker import parse_marker_number, write_paper_number_marker

_PAPER_NUMBER_RE = PAPER_NUMBER_RE
LEDGER_ALLOCATING = "allocating"
LEDGER_RESERVED = "reserved"
LEDGER_METADATA_STAGED = LEDGER_RESERVED
LEDGER_ABANDONED = "abandoned"

def now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")

def _read_json(path: Path, default: dict | None = None) -> dict:
    if not path.exists():
        return default or {}
    return json.loads(path.read_text(encoding="utf-8"))

class PaperNumberLedger:
    def __init__(self, path: str | Path = PAPER_NUMBER_LEDGER_PATH):
        self.path = Path(path)

    @property
    def _lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".lock")

    @staticmethod
    def empty_data() -> dict:
        return {"schema_version": "1.0", "max_number": "0000000000000000", "items": {}}

    @staticmethod
    def parse_marker_number(marker: Path) -> str | None:
        return parse_marker_number(marker)

    @staticmethod
    def write_marker(
        folder: str | Path,
        number: str,
        *,
        state: str,
        planned_paper_id: str = "",
    ) -> None:
        write_paper_number_marker(folder, number, state=state, planned_paper_id=planned_paper_id)

    @staticmethod
    def assert_papers_empty(papers_dir: str | Path) -> None:
        papers_dir = Path(papers_dir)
        if not papers_dir.exists():
            return
        formal_dirs = [p for p in papers_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
        if formal_dirs:
            names = ", ".join(p.name for p in formal_dirs[:5])
            more = "" if len(formal_dirs) <= 5 else f" (+{len(formal_dirs) - 5} more)"
            raise RuntimeError(f"data/papers is not empty; refusing paper_number reset: {names}{more}")

    def load(self) -> dict:
        data = _read_json(self.path, self.empty_data())
        base = self.empty_data()
        base.update(data)
        if not isinstance(base.get("items"), dict):
            base["items"] = {}
        # Backward-compat: ledger entries written before the reserve/activate
        # state machine have no ``state`` field; treat them as active.
        for item in base["items"].values():
            if isinstance(item, dict) and not item.get("state"):
                item["state"] = "active"
        return base

    def save(self, data: dict) -> None:
        with FileLock(str(self._lock_path)):
            self._save_unlocked(data)

    def _save_unlocked(self, data: dict) -> None:
        """Write ledger JSON atomically with fsync (caller holds lock)."""
        from src.utils.atomic_io import _fsync_dir

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write(json.dumps(data, ensure_ascii=False, indent=2))
                fh.flush()
                os.fsync(fh.fileno())
            json.loads(tmp.read_text(encoding="utf-8"))
            os.replace(tmp, self.path)
            _fsync_dir(self.path.parent)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def paper_number_for(self, folder: Path) -> str | None:
        folder_norm = normalize_repo_path(folder)
        for number, item in self.load().get("items", {}).items():
            if item.get("folder_path") == folder_norm or item.get("folder_name") == folder.name:
                return number
        return None

    def paper_number_from_marker(self, folder: str | Path) -> str | None:
        """Return the 16-digit number from the folder's ``*.paper.number`` marker, or None."""
        folder = Path(folder)
        for marker in folder.glob("*.paper.number"):
            candidate = self.parse_marker_number(marker)
            if candidate:
                return candidate
        return None

    def reset_empty_ledger(self, *, reason: str = "", reset_at: str | None = None) -> dict:
        """Reset the allocator ledger to an empty monotonic state."""
        with FileLock(str(self._lock_path)):
            data = self.load()
            reset_at = reset_at or now_iso()
            history = list(data.get("reset_history") or [])
            if reason:
                history.append({"reset_at": reset_at, "reason": reason})
            new_data = self.empty_data()
            if history:
                new_data["reset_history"] = history
            self._save_unlocked(new_data)
            return new_data

    @staticmethod
    def scan_paper_raw_number_floor(paper_raw_dir: str | Path, ledger_data: dict) -> int:
        """Return the monotonic floor for the next paper_raw allocation."""
        paper_raw_dir = Path(paper_raw_dir)
        numbers: list[int] = []

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if _PAPER_NUMBER_RE.match(text):
                numbers.append(int(text))

        add((ledger_data or {}).get("max_number"))
        items = (ledger_data or {}).get("items") or {}
        if isinstance(items, dict):
            for key in items:
                add(key)
        if paper_raw_dir.exists():
            for folder in paper_raw_dir.iterdir():
                if folder.is_dir():
                    add(folder.name)
            for marker in paper_raw_dir.glob("**/*.paper.number"):
                parsed = PaperNumberLedger.parse_marker_number(marker)
                if parsed:
                    add(parsed)
        return max(numbers, default=0)

    @staticmethod
    def classify_paper_raw_workspace(folder: str | Path, expected_number: str = "") -> dict[str, Any]:
        folder = Path(folder)
        expected_number = str(expected_number or "").strip()
        exists = folder.exists() and folder.is_dir()
        markers = sorted(folder.glob("*.paper.number")) if exists else []
        marker_number = PaperNumberLedger.parse_marker_number(markers[0]) if markers else ""
        marker_number = marker_number or ""
        number = expected_number or marker_number or (folder.name if _PAPER_NUMBER_RE.match(folder.name) else "")
        metadata_path = folder / f"{number}.metadata.json" if number else None
        has_metadata = bool(metadata_path and metadata_path.exists()) or (exists and any(folder.glob("*.metadata.json")))
        has_marker = bool(markers)
        has_stage_manifest = exists and (folder / "stage_manifest.json").exists()
        has_pdf = exists and any(folder.glob("*.pdf"))
        has_md = exists and any(folder.glob("*.md"))
        has_conversion = exists and any(folder.glob("*.conversion.json"))
        has_catalog = exists and any(folder.glob("*.catalog.json"))
        has_asset_manifest = exists and any(folder.glob("*.asset_manifest.json"))
        children = list(folder.iterdir()) if exists else []
        if exists and not children:
            classification = "empty_orphan_dir"
        elif has_metadata and has_marker and has_stage_manifest and not has_pdf and not has_md:
            classification = "metadata_only_workspace"
        elif has_marker and not has_metadata:
            classification = "marker_only_reserved"
        elif has_pdf:
            classification = "pdf_workspace"
        elif has_md or has_conversion:
            classification = "converted_workspace"
        elif has_metadata:
            classification = "metadata_workspace"
        elif exists:
            classification = "unknown_nonempty"
        else:
            classification = "missing_folder_for_ledger_item"
        return {
            "folder": str(folder),
            "folder_name": folder.name,
            "paper_number": number,
            "marker_number": marker_number,
            "classification": classification,
            "has_marker": has_marker,
            "has_metadata": has_metadata,
            "has_stage_manifest": has_stage_manifest,
            "has_pdf": has_pdf,
            "has_markdown": has_md,
            "has_catalog": has_catalog,
            "has_asset_manifest": has_asset_manifest,
            "child_count": len(children),
        }

    def _recover_allocating_items_unlocked(self, data: dict, paper_raw_dir: Path) -> None:
        items = data.setdefault("items", {})
        for number, item in list(items.items()):
            if not isinstance(item, dict):
                continue
            if str(item.get("state") or "") != LEDGER_ALLOCATING:
                continue
            stored = str(item.get("folder_path") or "")
            folder = resolve_stored_path(stored) if stored else paper_raw_dir / number
            info = self.classify_paper_raw_workspace(folder, number)
            created_at = item.get("created_at") or now_iso()
            base = {
                "folder_name": folder.name,
                "folder_path": normalize_repo_path(folder),
                "planned_paper_id": item.get("planned_paper_id") or "",
                "created_at": created_at,
                "recovered_at": now_iso(),
                "recovery_classification": info["classification"],
            }
            if info["classification"] in {"missing_folder_for_ledger_item", "empty_orphan_dir"}:
                items[number] = {
                    **base,
                    "state": LEDGER_ABANDONED,
                    "abandoned_at": now_iso(),
                    "abandoned_reason": f"recovered {info['classification']}",
                }
            elif info["classification"] == "marker_only_reserved":
                items[number] = {**base, "state": LEDGER_RESERVED}
            elif info.get("has_metadata"):
                items[number] = {**base, "state": LEDGER_METADATA_STAGED}

    def reserve_next_for_paper_raw_workspace(
        self,
        paper_raw_dir: str | Path,
        *,
        planned_paper_id: str = "",
    ) -> tuple[str, Path]:
        """Reserve the next paper_number and create ``paper_raw/<number>``.

        This method is ledger-first and monotonic-first. The caller should hold
        ``paper_raw/.paper_raw_write.lock`` for complete ingest write
        transactions; this method holds the ledger lock only for short ledger
        updates and never while copying large files.
        """
        paper_raw_dir = Path(paper_raw_dir)
        paper_raw_dir.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self._lock_path)):
            data = self.load()
            self._recover_allocating_items_unlocked(data, paper_raw_dir)
            number = f"{self.scan_paper_raw_number_floor(paper_raw_dir, data) + 1:016d}"
            folder = safe_child(paper_raw_dir, number)
            data["max_number"] = number
            data.setdefault("items", {})[number] = {
                "folder_name": folder.name,
                "folder_path": normalize_repo_path(folder),
                "planned_paper_id": planned_paper_id,
                "state": LEDGER_ALLOCATING,
                "created_at": now_iso(),
            }
            self._save_unlocked(data)

        try:
            folder.mkdir(parents=False, exist_ok=False)
            self.write_marker(folder, number, state=LEDGER_RESERVED, planned_paper_id=planned_paper_id)
        except Exception as exc:
            self.mark_abandoned(number, str(exc), folder=folder)
            raise

        with FileLock(str(self._lock_path)):
            data = self.load()
            items = data.setdefault("items", {})
            existing = items.get(number) or {}
            items[number] = {
                "folder_name": folder.name,
                "folder_path": normalize_repo_path(folder),
                "planned_paper_id": planned_paper_id or existing.get("planned_paper_id") or "",
                "state": LEDGER_RESERVED,
                "created_at": existing.get("created_at") or now_iso(),
                "reserved_at": now_iso(),
            }
            self._save_unlocked(data)
        return number, folder

    def peek_next_numbers(self, count: int) -> list[str]:
        """Return the next ``count`` paper_numbers without mutating the ledger."""
        if count < 0:
            raise ValueError("count must be non-negative")
        data = self.load()
        start = int(data.get("max_number") or "0") + 1
        return [f"{start + i:016d}" for i in range(count)]

    def reserve_for_paper_raw(self, source_folder: str | Path, planned_paper_id: str = "") -> str:
        """Reserve the next 16-digit paper_number for a paper_raw workspace.

        Idempotent: if ``source_folder`` already has a ``*.paper.number`` marker,
        that number is reused. Writes the marker into ``source_folder`` and a
        ledger item with ``state="reserved"`` pointing at the paper_raw folder.

        Uses ``scan_paper_raw_number_floor`` (not just ``max_number + 1``) so
        that orphan directories and markers contribute to the monotonic floor.
        This method only holds the ledger lock briefly — callers are
        responsible for any higher-level locking (e.g. ``paper_raw_write.lock``
        for full ingest write transactions).
        """
        source_folder = Path(source_folder)
        existing = self.paper_number_from_marker(source_folder)
        if existing:
            return existing
        with FileLock(str(self._lock_path)):
            data = self.load()
            paper_raw_root = source_folder.parent
            number_floor = self.scan_paper_raw_number_floor(paper_raw_root, data)
            number = f"{number_floor + 1:016d}"
            data["max_number"] = number
            data.setdefault("items", {})[number] = {
                "folder_name": source_folder.name,
                "folder_path": normalize_repo_path(source_folder),
                "planned_paper_id": planned_paper_id,
                "state": "reserved",
                "created_at": now_iso(),
            }
            self._save_unlocked(data)
            self.write_marker(source_folder, number, state="reserved", planned_paper_id=planned_paper_id)
            return number

    def reserve_specific_for_paper_raw(
        self,
        number: str,
        folder: str | Path,
        *,
        planned_paper_id: str = "",
    ) -> str:
        """Reserve a specific 16-digit number for a paper_raw workspace.

        This is the formalize-time counterpart to ``reserve_for_paper_raw`` for
        repair/import flows that must preserve a known number. It never creates
        an active ledger entry; active numbers and numbers reserved for another
        folder are rejected.
        """
        if not _PAPER_NUMBER_RE.match(str(number or "")):
            raise ValueError(f"invalid paper_number: {number}")
        folder = Path(folder)
        folder_norm = normalize_repo_path(folder)
        with FileLock(str(self._lock_path)):
            data = self.load()
            items = data.setdefault("items", {})
            existing = items.get(number)
            if existing:
                state = existing.get("state") or "active"
                existing_path = existing.get("folder_path") or ""
                same_folder = (
                    existing_path == folder_norm
                    or (not existing_path and existing.get("folder_name") == folder.name)
                )
                if state == "active":
                    raise ValueError(f"paper_number already active: {number}")
                if state != "reserved":
                    raise ValueError(f"cannot reserve number {number} in state {state}")
                if not same_folder:
                    raise ValueError(f"paper_number already reserved for another folder: {number}")
                created_at = existing.get("created_at") or now_iso()
            else:
                if int(number) > int(str(data.get("max_number") or "0000000000000000")):
                    data["max_number"] = number
                created_at = now_iso()
            planned = planned_paper_id or (existing or {}).get("planned_paper_id") or ""
            items[number] = {
                "folder_name": folder.name,
                "folder_path": folder_norm,
                "planned_paper_id": planned,
                "state": "reserved",
                "created_at": created_at,
            }
            self._save_unlocked(data)
            self.write_marker(folder, number, state="reserved", planned_paper_id=planned)
            return number

    def move_reserved_workspace_for_migration(
        self,
        number: str,
        folder: str | Path,
        *,
        planned_paper_id: str = "",
    ) -> str:
        """Removed legacy hook; active callers must never rename raw workspaces.

        Used by ``formalize`` after renaming ``000001`` → ``<paper_id>``: the
        number was reserved against the 6-digit source folder, and this updates
        the ledger entry + marker to point at the renamed ``<paper_id>`` folder
        while keeping ``state="reserved"``. Requires the number to already exist
        in the ledger (raises otherwise, to avoid hiding reservation errors).
        Unlike ``repoint()``, this writes the full marker
        (paper_number/folder_name/state/planned_paper_id).
        """
        if not _PAPER_NUMBER_RE.match(str(number or "")):
            raise ValueError(f"invalid paper_number: {number}")
        folder = Path(folder)
        with FileLock(str(self._lock_path)):
            data = self.load()
            items = data.setdefault("items", {})
            if number not in items:
                raise KeyError(f"paper_number not in ledger: {number}")
            existing = items[number] or {}
            items[number] = {
                "folder_name": folder.name,
                "folder_path": normalize_repo_path(folder),
                "planned_paper_id": planned_paper_id or existing.get("planned_paper_id") or "",
                "state": "reserved",
                "created_at": existing.get("created_at") or now_iso(),
                "repointed_at": now_iso(),
            }
            self._save_unlocked(data)
            self.write_marker(
                folder,
                number,
                state="reserved",
                planned_paper_id=planned_paper_id or existing.get("planned_paper_id") or "",
            )
            return number

    def quarantine_reserved_duplicate(
        self,
        number: str,
        folder: str | Path,
        *,
        duplicate_of: str = "",
        duplicate_reasons: list[str] | None = None,
    ) -> str:
        """Mark a reserved paper_raw workspace as quarantined duplicate."""
        if not _PAPER_NUMBER_RE.match(str(number or "")):
            raise ValueError(f"invalid paper_number: {number}")
        folder = Path(folder)
        with FileLock(str(self._lock_path)):
            data = self.load()
            items = data.setdefault("items", {})
            existing = items.get(number) or {}
            state = existing.get("state") or "reserved"
            if state not in {"reserved", "quarantined_duplicate"}:
                raise ValueError(f"cannot quarantine number {number} in state {state}")
            items[number] = {
                "folder_name": folder.name,
                "folder_path": normalize_repo_path(folder),
                "planned_paper_id": existing.get("planned_paper_id") or "",
                "state": LEDGER_ABANDONED,
                "created_at": existing.get("created_at") or now_iso(),
                "quarantined_at": now_iso(),
                "quarantined_duplicate_of": duplicate_of,
                "duplicate_reasons": list(duplicate_reasons or []),
            }
            self._save_unlocked(data)
            self.write_marker(
                folder,
                number,
                state=LEDGER_ABANDONED,
                planned_paper_id=existing.get("planned_paper_id") or "",
            )
            return number

    def mark_stage_failed(
        self,
        number: str,
        folder: str | Path,
        *,
        errors: list[str] | None = None,
    ) -> str:
        """Mark a reserved staging workspace as failed but auditable."""
        if not _PAPER_NUMBER_RE.match(str(number or "")):
            raise ValueError(f"invalid paper_number: {number}")
        folder = Path(folder)
        with FileLock(str(self._lock_path)):
            data = self.load()
            items = data.setdefault("items", {})
            existing = items.get(number) or {}
            state = existing.get("state") or LEDGER_RESERVED
            if state not in {LEDGER_ALLOCATING, LEDGER_RESERVED, LEDGER_METADATA_STAGED, STAGE_FAILED}:
                raise ValueError(f"cannot mark stage_failed for number {number} in state {state}")
            items[number] = {
                "folder_name": folder.name,
                "folder_path": normalize_repo_path(folder),
                "planned_paper_id": existing.get("planned_paper_id") or "",
                "state": LEDGER_RESERVED,
                "created_at": existing.get("created_at") or now_iso(),
                "stage_failed_at": now_iso(),
                "errors": list(errors or []),
            }
            self._save_unlocked(data)
            if folder.exists():
                self.write_marker(
                    folder,
                    number,
                    state=LEDGER_RESERVED,
                    planned_paper_id=existing.get("planned_paper_id") or "",
                )
            return number

    def mark_abandoned(
        self,
        number: str,
        reason: str,
        *,
        folder: str | Path | None = None,
    ) -> str:
        """Mark an interrupted allocation as abandoned without recycling it."""
        if not _PAPER_NUMBER_RE.match(str(number or "")):
            raise ValueError(f"invalid paper_number: {number}")
        folder_path = Path(folder) if folder is not None else Path("")
        with FileLock(str(self._lock_path)):
            data = self.load()
            items = data.setdefault("items", {})
            existing = items.get(number) or {}
            state = existing.get("state") or LEDGER_ALLOCATING
            if state not in {LEDGER_ALLOCATING, LEDGER_RESERVED, LEDGER_ABANDONED, STAGE_FAILED}:
                raise ValueError(f"cannot mark abandoned for number {number} in state {state}")
            stored_path = str(existing.get("folder_path") or "")
            target_folder = folder_path if str(folder_path) else (resolve_stored_path(stored_path) if stored_path else Path(""))
            items[number] = {
                "folder_name": target_folder.name if str(target_folder) else existing.get("folder_name") or number,
                "folder_path": normalize_repo_path(target_folder) if str(target_folder) else existing.get("folder_path") or "",
                "planned_paper_id": existing.get("planned_paper_id") or "",
                "state": LEDGER_ABANDONED,
                "created_at": existing.get("created_at") or now_iso(),
                "abandoned_at": now_iso(),
                "abandoned_reason": reason,
            }
            self._save_unlocked(data)
            if str(target_folder) and target_folder.exists() and any(target_folder.iterdir()):
                try:
                    self.write_marker(
                        target_folder,
                        number,
                        state=LEDGER_ABANDONED,
                        planned_paper_id=existing.get("planned_paper_id") or "",
                    )
                except Exception:
                    pass
            return number

    def mark_metadata_staged(self, number: str, folder: str | Path) -> str:
        """Mark a reserved paper_raw workspace after metadata/stage files exist.

        Accepts the normal forward transitions (``allocating``/``reserved`` →
        ``metadata_staged``) plus the recovery transitions
        ``stage_failed``/``abandoned`` → ``metadata_staged``. The recovery
        transitions are only safe because the caller is completing the SAME
        in-progress allocation for the SAME candidate (identified by discovery
        context), never recycling a hole for a different candidate.
        """
        if not _PAPER_NUMBER_RE.match(str(number or "")):
            raise ValueError(f"invalid paper_number: {number}")
        folder = Path(folder)
        with FileLock(str(self._lock_path)):
            data = self.load()
            items = data.setdefault("items", {})
            existing = items.get(number) or {}
            state = existing.get("state") or LEDGER_RESERVED
            allowed = {
                LEDGER_ALLOCATING,
                LEDGER_RESERVED,
                LEDGER_METADATA_STAGED,
                STAGE_FAILED,
                LEDGER_ABANDONED,
            }
            if state not in allowed:
                raise ValueError(f"cannot mark metadata_staged for number {number} in state {state}")
            items[number] = {
                "folder_name": folder.name,
                "folder_path": normalize_repo_path(folder),
                "planned_paper_id": existing.get("planned_paper_id") or "",
                "state": LEDGER_RESERVED,
                "created_at": existing.get("created_at") or now_iso(),
                "metadata_staged_at": now_iso(),
            }
            self._save_unlocked(data)
            if folder.exists():
                self.write_marker(
                    folder,
                    number,
                    state=LEDGER_RESERVED,
                    planned_paper_id=existing.get("planned_paper_id") or "",
                )
            return number

    def activate_reserved(
        self,
        number: str,
        final_folder: str | Path,
        paper_id: str = "",
    ) -> str:
        """Flip a reserved number to ``active`` and repoint it at the formal library folder.

        Used by ``commit_paper_raw`` after ``os.replace`` installs the formal
        copy. The marker (already copied by copytree) is rewritten with
        ``state="active"``. The number MUST already exist in the ledger as a
        ``reserved`` entry (formalize reserves before commit); a missing entry
        raises ``KeyError`` so commit fails+rolls back rather than silently
        masking a formalize/ledger bug.
        """
        if not _PAPER_NUMBER_RE.match(str(number or "")):
            raise ValueError(f"invalid paper_number: {number}")
        final_folder = Path(final_folder)
        with FileLock(str(self._lock_path)):
            data = self.load()
            items = data.setdefault("items", {})
            if number not in items:
                raise KeyError(f"paper_number not in ledger: {number}")
            existing = items[number] or {}
            state = existing.get("state") or "reserved"
            if state != "reserved":
                raise ValueError(f"cannot activate number {number} in state {state}")
            if int(number) > int(str(data.get("max_number") or "0000000000000000")):
                data["max_number"] = number
            items[number] = {
                "folder_name": final_folder.name,
                "folder_path": normalize_repo_path(final_folder),
                "planned_paper_id": existing.get("planned_paper_id") or paper_id,
                "paper_id": paper_id,
                "state": "active",
                "created_at": existing.get("created_at") or now_iso(),
                "activated_at": now_iso(),
            }
            self._save_unlocked(data)
            self.write_marker(
                final_folder,
                number,
                state="active",
                planned_paper_id=paper_id or existing.get("planned_paper_id") or "",
            )
            return number

    def activate_reserved_locked(
        self,
        number: str,
        final_folder: str | Path,
        paper_id: str = "",
    ) -> str:
        """Same as ``activate_reserved`` but caller must hold the ledger lock.

        No internal ``FileLock`` acquisition — the transaction coordinator
        is expected to have already acquired the ledger lock through the
        ranked-lock system (``LEDGER_RANK``).  Calling this without holding
        the lock can race against other ledger writers.
        """
        if not _PAPER_NUMBER_RE.match(str(number or "")):
            raise ValueError(f"invalid paper_number: {number}")
        final_folder = Path(final_folder)
        data = self.load()
        items = data.setdefault("items", {})
        if number not in items:
            raise KeyError(f"paper_number not in ledger: {number}")
        existing = items[number] or {}
        state = existing.get("state") or "reserved"
        if state != "reserved":
            raise ValueError(f"cannot activate number {number} in state {state}")
        if int(number) > int(str(data.get("max_number") or "0000000000000000")):
            data["max_number"] = number
        items[number] = {
            "folder_name": final_folder.name,
            "folder_path": normalize_repo_path(final_folder),
            "planned_paper_id": existing.get("planned_paper_id") or paper_id,
            "paper_id": paper_id,
            "state": "active",
            "created_at": existing.get("created_at") or now_iso(),
            "activated_at": now_iso(),
        }
        self._save_unlocked(data)
        self.write_marker(
            final_folder,
            number,
            state="active",
            planned_paper_id=paper_id or existing.get("planned_paper_id") or "",
        )
        return number

    def deactivate_to_source(self, number: str, source_folder: str | Path) -> str:
        """Roll an activated number back to ``reserved`` pointing at paper_raw.

        Used by ``commit_paper_raw`` rollback when a post-install step fails:
        the formal copy is removed and the reserved number is reattached to
        the still-present paper_raw source so formalize→commit can be retried.
        """
        if not _PAPER_NUMBER_RE.match(str(number or "")):
            raise ValueError(f"invalid paper_number: {number}")
        source_folder = Path(source_folder)
        with FileLock(str(self._lock_path)):
            data = self.load()
            items = data.setdefault("items", {})
            existing = items.get(number) or {}
            items[number] = {
                "folder_name": source_folder.name,
                "folder_path": normalize_repo_path(source_folder),
                "planned_paper_id": existing.get("planned_paper_id") or "",
                "state": "reserved",
                "created_at": existing.get("created_at") or now_iso(),
                "activated_at": existing.get("activated_at") or "",
                "deactivated_at": now_iso(),
            }
            self._save_unlocked(data)
            return number

    def rollback_active_to_reserved(
        self,
        number: str,
        raw_folder: str | Path,
        *,
        planned_paper_id: str = "",
    ) -> str:
        """Rollback a formal-library active number to a paper_raw reservation.

        This is used by the explicit formal-library rollback tool. It preserves
        the monotonic ledger state (including max_number and existing timestamps)
        while repointing the item at ``data/paper_raw/<paper_number>``.
        """
        if not _PAPER_NUMBER_RE.match(str(number or "")):
            raise ValueError(f"invalid paper_number: {number}")
        raw_folder = Path(raw_folder)
        with FileLock(str(self._lock_path)):
            data = self.load()
            items = data.setdefault("items", {})
            if number not in items:
                raise KeyError(f"paper_number not in ledger: {number}")
            existing = items[number] or {}
            state = existing.get("state") or "active"
            if state != "active":
                raise ValueError(f"cannot rollback number {number} in state {state}")
            planned = planned_paper_id or existing.get("planned_paper_id") or existing.get("paper_id") or ""
            items[number] = {
                "folder_name": raw_folder.name,
                "folder_path": normalize_repo_path(raw_folder),
                "planned_paper_id": planned,
                "state": "reserved",
                "created_at": existing.get("created_at") or now_iso(),
                "activated_at": existing.get("activated_at") or "",
                "rolled_back_at": now_iso(),
            }
            self._save_unlocked(data)
            self.write_marker(raw_folder, number, state="reserved", planned_paper_id=planned)
            return number

    def rollback_active_to_reserved_locked(
        self,
        number: str,
        raw_folder: str | Path,
        *,
        planned_paper_id: str = "",
    ) -> str:
        """Same as ``rollback_active_to_reserved`` but caller must hold the ledger lock.

        No internal ``FileLock`` acquisition — the transaction coordinator
        is expected to have already acquired the ledger lock through the
        ranked-lock system (``LEDGER_RANK``).
        """
        if not _PAPER_NUMBER_RE.match(str(number or "")):
            raise ValueError(f"invalid paper_number: {number}")
        raw_folder = Path(raw_folder)
        data = self.load()
        items = data.setdefault("items", {})
        if number not in items:
            raise KeyError(f"paper_number not in ledger: {number}")
        existing = items[number] or {}
        state = existing.get("state") or "active"
        if state != "active":
            raise ValueError(f"cannot rollback number {number} in state {state}")
        planned = planned_paper_id or existing.get("planned_paper_id") or existing.get("paper_id") or ""
        items[number] = {
            "folder_name": raw_folder.name,
            "folder_path": normalize_repo_path(raw_folder),
            "planned_paper_id": planned,
            "state": "reserved",
            "created_at": existing.get("created_at") or now_iso(),
            "activated_at": existing.get("activated_at") or "",
            "rolled_back_at": now_iso(),
        }
        self._save_unlocked(data)
        self.write_marker(raw_folder, number, state="reserved", planned_paper_id=planned)
        return number

    def validate(self, papers_dir: str | Path = PAPERS_DIR) -> tuple[list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        data = self.load()
        for number, item in data.get("items", {}).items():
            if not _PAPER_NUMBER_RE.match(number):
                errors.append(f"invalid paper_number: {number}")
            state = (item or {}).get("state") or "active"
            folder = resolve_stored_path(item.get("folder_path") or "")
            if not folder.exists():
                if state == "active":
                    errors.append(f"active ledger folder missing: {number} {folder}")
                else:
                    # A reserved number whose paper_raw folder is gone is an
                    # orphan (recoverable via audit / re-formalize), not hard
                    # corruption.
                    warnings.append(f"ledger folder missing: {number} {folder}")
                continue
            markers = list(folder.glob("*.paper.number"))
            if not markers:
                # An active (formal) entry whose folder exists but is missing
                # the paper.number marker is corrupt — the marker is required.
                if state == "active":
                    errors.append(f"active number missing marker: {number} {folder}")
                else:
                    warnings.append(f"reserved number missing marker: {number} {folder}")
                continue
            if markers[0].name != f"{number}.paper.number":
                marker_number = self.parse_marker_number(markers[0]) or markers[0].name
                errors.append(f"ledger/marker conflict for {folder.name}: {number} vs {marker_number}")
        return errors, warnings
