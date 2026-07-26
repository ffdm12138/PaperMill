"""Permanent paper-number ledger and marker lifecycle helpers."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from filelock import FileLock
import orjson

from config.settings import PAPER_NUMBER_LEDGER_PATH, PAPERS_DIR
from src.path_utils import normalize_repo_path, resolve_stored_path
from src.naming import safe_child
from src.utils.identifiers import PAPER_NUMBER_RE
from src.utils.atomic_io import atomic_write_json
from src.ingest.marker import parse_marker_number, write_paper_number_marker
from src.utils.timestamps import now_iso
from src.library.paper_number_state import (
    ALLOWED_LEDGER_TRANSITIONS,
    ALL_LEDGER_STATES,
    InvalidLedgerTransition,
    LEDGER_ABANDONED,
    LEDGER_ACTIVE,
    LEDGER_ALLOCATING,
    LEDGER_METADATA_STAGED,
    LEDGER_RESERVED,
    assert_ledger_transition,
    assert_ledger_repair_transition,
    build_transition_patch,
)

_PAPER_NUMBER_RE = PAPER_NUMBER_RE

# Workspaces whose staging is confirmed complete (metadata + source record +
# receipt + manifest persisted). ``reserved`` means the workspace + marker exist
# but staging is NOT yet confirmed complete — it is "unsettled" from the index
# perspective and must be re-scanned until it reaches ``metadata_staged``.
# Historical ledger data written before this split stored ``"reserved"`` for
# metadata-staged workspaces; preflight/repair reclassifies complete ``reserved``
# workspaces to ``metadata_staged`` (only under explicit ``--apply``).


def _read_json(path: Path, default: dict | None = None) -> dict:
    if not path.exists():
        return default or {}
    return orjson.loads(path.read_bytes())


class LockedLedgerSession:
    """One ledger load and explicit durable checkpoints under the ledger lock.

    The caller must already hold ``paper_raw/.paper_raw_write.lock``.  This
    session is intended for the metadata-only discovery transaction; it must
    not span PDF or directory-tree copies.
    """

    def __init__(self, ledger: "PaperNumberLedger", *, observer: Any = None) -> None:
        self.ledger = ledger
        self.observer = observer
        self.data: dict[str, Any] = {}
        self.dirty = False
        self._lock = FileLock(str(ledger._lock_path))

    def __enter__(self) -> "LockedLedgerSession":
        self._lock.acquire()
        try:
            self.data = self.ledger.load()
            if self.observer is not None:
                self.observer.ledger_load()
            return self
        except Exception:
            self._lock.release()
            raise

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._lock.release()

    def reserve_number(self, paper_raw_dir: str | Path, *, planned_paper_name: str = "") -> tuple[str, Path]:
        """Create marker/workspace and persist the reserved checkpoint once."""
        if self.dirty:
            raise RuntimeError("unsaved ledger mutation before reserve")
        root = Path(paper_raw_dir)
        root.mkdir(parents=True, exist_ok=True)
        current_max = int(str(self.data.get("max_number") or "0"))
        number = f"{current_max + 1:016d}"
        items = self.data.setdefault("items", {})
        if number in items:
            raise RuntimeError(
                f"paper_number_counter_collision: {number} already exists in ledger"
            )
        folder = safe_child(root, number)
        folder.mkdir(parents=False, exist_ok=False)
        self.ledger.write_marker(
            folder, number, state=LEDGER_RESERVED, planned_paper_name=planned_paper_name)
        timestamp = now_iso()
        self.data["max_number"] = number
        items[number] = {
            "folder_name": folder.name,
            "folder_path": normalize_repo_path(folder),
            "planned_paper_name": planned_paper_name,
            "state": LEDGER_RESERVED,
            "created_at": timestamp,
            "reserved_at": timestamp,
        }
        self.dirty = True
        self.save_checkpoint()
        return number, folder

    def transition_metadata_staged(self, number: str, folder: str | Path) -> None:
        """Mutate the loaded view after all staging evidence has validated."""
        folder = Path(folder)
        items = self.data.setdefault("items", {})
        existing = items.get(number)
        if not isinstance(existing, dict):
            raise KeyError(f"paper_number not in locked ledger view: {number}")
        state = str(existing.get("state") or "")
        if state == LEDGER_METADATA_STAGED:
            return
        assert_ledger_transition(
            paper_number=number, current_state=state,
            target_state=LEDGER_METADATA_STAGED)
        self.ledger.write_marker(
            folder, number, state=LEDGER_METADATA_STAGED,
            planned_paper_name=str(existing.get("planned_paper_name") or ""))
        items[number] = build_transition_patch(
            existing, number=number, target_state=LEDGER_METADATA_STAGED,
            folder=str(folder), folder_path=normalize_repo_path(folder),
            planned_paper_name=str(existing.get("planned_paper_name") or ""),
            now_iso=now_iso(),
        )
        self.dirty = True

    def save_checkpoint(self) -> None:
        if not self.dirty:
            return
        self.ledger._save_unlocked(self.data)
        self.dirty = False
        if self.observer is not None:
            self.observer.ledger_save()


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
        planned_paper_name: str = "",
        fsync: bool = True,
    ) -> None:
        write_paper_number_marker(folder, number, state=state,
                                  planned_paper_name=planned_paper_name, fsync=fsync)

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
            with tmp.open("wb") as fh:
                # The ledger is machine state, not a hand-edited document.
                # Compact encoding materially reduces both serialization and
                # fsync cost for large ledgers while preserving the same two
                # crash-safe checkpoints.
                fh.write(orjson.dumps(data))
                fh.flush()
                os.fsync(fh.fileno())
            orjson.loads(tmp.read_bytes())
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
        """Recover ``allocating`` items using unified lifecycle inspection.

        Rules (in priority order):
        - missing/empty workspace → abandoned
        - completely unreadable (no marker, no metadata) → abandoned
        - complete metadata staging artifacts → metadata_staged
        - marker-only (no metadata) → reserved
        - metadata exists but staging incomplete → reserved (kept unsettled)
        - unclear → reserved, do not auto-promote
        - Never upgrades to metadata_staged solely based on metadata file
          existence.
        - Staging failure is an operation result — never written to the ledger
          as a state. Incomplete workspaces stay ``reserved`` so the index
          re-scans them.
        """
        from src.ingest.workspace_lifecycle import inspect_workspace_lifecycle

        items = data.setdefault("items", {})
        for number, item in list(items.items()):
            if not isinstance(item, dict):
                continue
            if str(item.get("state") or "") != LEDGER_ALLOCATING:
                continue
            stored = str(item.get("folder_path") or "")
            folder = resolve_stored_path(stored) if stored else paper_raw_dir / number
            created_at = item.get("created_at") or now_iso()
            base = {
                "folder_name": folder.name,
                "folder_path": normalize_repo_path(folder),
                "planned_paper_name": item.get("planned_paper_name") or "",
                "created_at": created_at,
                "recovered_at": now_iso(),
            }

            if not folder.exists() or (folder.exists() and not any(folder.iterdir())):
                # missing or empty → abandoned
                items[number] = {
                    **base,
                    "state": LEDGER_ABANDONED,
                    "abandoned_at": now_iso(),
                    "abandoned_reason": (
                        "recovered missing_folder_for_ledger_item"
                        if not folder.exists()
                        else "recovered empty_orphan_dir"
                    ),
                }
                continue

            inspection = inspect_workspace_lifecycle(folder, ledger_item=item)

            if inspection.repair_required and not inspection.metadata_valid and not inspection.marker_valid:
                # Completely unreadable → abandoned (can't even identify it).
                items[number] = {
                    **base,
                    "state": LEDGER_ABANDONED,
                    "abandoned_at": now_iso(),
                    "abandoned_reason": f"recovered unreadable workspace: {'; '.join(inspection.errors[:3])}",
                }
                continue

            if inspection.readiness.ready:
                items[number] = {
                    **base,
                    "state": LEDGER_METADATA_STAGED,
                    "metadata_staged_at": now_iso(),
                }
            elif inspection.marker_valid and not inspection.metadata_valid:
                # Marker-only → reserved.
                items[number] = {**base, "state": LEDGER_RESERVED, "reserved_at": now_iso()}
            else:
                # Metadata exists but staging incomplete, or unclear —
                # keep reserved, do not auto-promote. Staging failure is an
                # operation result stored in .import_status.json, not a ledger
                # state. The workspace stays reserved so the index re-scans it.
                items[number] = {**base, "state": LEDGER_RESERVED, "reserved_at": now_iso()}

    def reserve_next_for_paper_raw_workspace(
        self,
        paper_raw_dir: str | Path,
        *,
        planned_paper_name: str = "",
    ) -> tuple[str, Path]:
        """Reserve the next paper_number and create ``paper_raw/<number>``.

        This method is ledger-first and monotonic-first. The caller should hold
        ``paper_raw/.paper_raw_write.lock`` for complete ingest write
        transactions; this method holds the ledger lock only for short ledger
        updates and never while copying large files.

        Generic ingest retains the defensive recover + floor scan. Discovery
        metadata staging uses :class:`LockedLedgerSession` while holding the
        allocator write lock.
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
                "planned_paper_name": planned_paper_name,
                "state": LEDGER_ALLOCATING,
                "created_at": now_iso(),
            }
            self._save_unlocked(data)

        try:
            folder.mkdir(parents=False, exist_ok=False)
            self.write_marker(folder, number, state=LEDGER_RESERVED, planned_paper_name=planned_paper_name)
        except FileExistsError:
            # Directory collision: never silently try the next number. Abandon
            # this number and report paper_number_collision so an explicit
            # monotonic-floor repair can run.
            self.mark_abandoned(
                number, "paper_number_collision: directory already exists", folder=folder
            )
            raise RuntimeError(
                f"paper_number_collision: {folder} already exists; "
                "monotonic-floor repair required"
            )
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
                "planned_paper_name": planned_paper_name or existing.get("planned_paper_name") or "",
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

    def reserve_for_paper_raw(self, source_folder: str | Path, planned_paper_name: str = "") -> str:
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
                "planned_paper_name": planned_paper_name,
                "state": "reserved",
                "created_at": now_iso(),
            }
            self._save_unlocked(data)
            self.write_marker(source_folder, number, state="reserved", planned_paper_name=planned_paper_name)
            return number

    def reserve_specific_for_paper_raw(
        self,
        number: str,
        folder: str | Path,
        *,
        planned_paper_name: str = "",
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
            planned = planned_paper_name or (existing or {}).get("planned_paper_name") or ""
            items[number] = {
                "folder_name": folder.name,
                "folder_path": folder_norm,
                "planned_paper_name": planned,
                "state": "reserved",
                "created_at": created_at,
            }
            self._save_unlocked(data)
            self.write_marker(folder, number, state="reserved", planned_paper_name=planned)
            return number

    def quarantine_reserved_duplicate(
        self,
        number: str,
        folder: str | Path,
        *,
        duplicate_of: str = "",
        duplicate_reasons: list[str] | None = None,
    ) -> str:
        """Mark a paper_raw workspace as quarantined duplicate."""
        if not _PAPER_NUMBER_RE.match(str(number or "")):
            raise ValueError(f"invalid paper_number: {number}")
        folder = Path(folder)
        with FileLock(str(self._lock_path)):
            data = self.load()
            items = data.setdefault("items", {})
            existing = items.get(number) or {}
            state = existing.get("state") or "reserved"
            if state not in {"reserved", "metadata_staged"}:
                raise ValueError(f"cannot quarantine number {number} in state {state}")
            items[number] = {
                "folder_name": folder.name,
                "folder_path": normalize_repo_path(folder),
                "planned_paper_name": existing.get("planned_paper_name") or "",
                "state": LEDGER_ABANDONED,
                "created_at": existing.get("created_at") or now_iso(),
                "quarantined_at": now_iso(),
                "quarantine_reason": "duplicate_workspace",
                "quarantine_path": normalize_repo_path(folder),
                "quarantined_duplicate_of": duplicate_of,
                "duplicate_reasons": list(duplicate_reasons or []),
            }
            self._save_unlocked(data)
            self.write_marker(
                folder,
                number,
                state=LEDGER_ABANDONED,
                planned_paper_name=existing.get("planned_paper_name") or "",
            )
            return number

    def migrate_legacy_quarantined_duplicate(self, number: str) -> str:
        """Explicitly replace the retired temporary state with ``abandoned``."""
        if not _PAPER_NUMBER_RE.match(str(number or "")):
            raise ValueError(f"invalid paper_number: {number}")
        with FileLock(str(self._lock_path)):
            data = self.load()
            existing = (data.get("items") or {}).get(number)
            if not isinstance(existing, dict):
                raise KeyError(number)
            if existing.get("state") != "quarantined_duplicate":
                raise ValueError(f"legacy state not present for {number}")
            migrated = dict(existing)
            migrated.update({
                "state": LEDGER_ABANDONED,
                "abandoned_at": now_iso(),
                "abandoned_reason": "duplicate_workspace",
                "quarantine_reason": "duplicate_workspace",
                "quarantine_path": str(existing.get("folder_path") or ""),
            })
            data["items"][number] = migrated
            self._save_unlocked(data)
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
            if state not in {LEDGER_ALLOCATING, LEDGER_RESERVED, LEDGER_ABANDONED}:
                raise ValueError(f"cannot mark abandoned for number {number} in state {state}")
            stored_path = str(existing.get("folder_path") or "")
            target_folder = folder_path if str(folder_path) else (resolve_stored_path(stored_path) if stored_path else Path(""))
            items[number] = {
                "folder_name": target_folder.name if str(target_folder) else existing.get("folder_name") or number,
                "folder_path": normalize_repo_path(target_folder) if str(target_folder) else existing.get("folder_path") or "",
                "planned_paper_name": existing.get("planned_paper_name") or "",
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
                        planned_paper_name=existing.get("planned_paper_name") or "",
                    )
                except OSError:
                    # Marker write is best-effort; ledger state already persisted.
                    pass
            return number

    def mark_metadata_staged(
        self, number: str, folder: str | Path
    ) -> str:
        """Mark a paper_raw workspace as ``metadata_staged``.

        Accepts only the normal forward transitions: ``allocating``/``reserved`` →
        ``metadata_staged``. Idempotent if already ``metadata_staged``.

        ``abandoned`` numbers are permanently terminal and never revived.
        ``active`` workspaces must use :meth:`rollback_active_to_metadata_staged`
        for explicit rollbacks. Staging failure is recorded in
        ``.import_status.json``, not the ledger — incomplete workspaces stay
        ``reserved`` and are re-scanned.
        """
        if not _PAPER_NUMBER_RE.match(str(number or "")):
            raise ValueError(f"invalid paper_number: {number}")
        folder = Path(folder)
        with FileLock(str(self._lock_path)):
            data = self.load()
            items = data.setdefault("items", {})
            existing = items.get(number) or {}
            state = existing.get("state") or LEDGER_RESERVED

            if state == LEDGER_ACTIVE:
                raise InvalidLedgerTransition(
                    number, state, LEDGER_METADATA_STAGED,
                    reason="active workspaces must use rollback_active_to_metadata_staged",
                )
            if state == LEDGER_ABANDONED:
                raise InvalidLedgerTransition(
                    number, state, LEDGER_METADATA_STAGED,
                    reason="abandoned numbers are permanently terminal",
                )
            if state == LEDGER_METADATA_STAGED:
                return number  # idempotent

            # Normal forward: allocating/reserved → metadata_staged
            if state not in {LEDGER_ALLOCATING, LEDGER_RESERVED}:
                raise InvalidLedgerTransition(
                    number, state, LEDGER_METADATA_STAGED,
                )

            assert_ledger_transition(
                paper_number=number,
                current_state=state,
                target_state=LEDGER_METADATA_STAGED,
            )

            items[number] = build_transition_patch(
                existing,
                number=number,
                target_state=LEDGER_METADATA_STAGED,
                folder=str(folder),
                folder_path=normalize_repo_path(folder),
                planned_paper_name=existing.get("planned_paper_name", ""),
                now_iso=now_iso(),
            )
            self._save_unlocked(data)
            if folder.exists():
                self.write_marker(
                    folder,
                    number,
                    state=LEDGER_METADATA_STAGED,
                    planned_paper_name=existing.get("planned_paper_name") or "",
                )
            return number

    def demote_metadata_staged_to_reserved(
        self, number: str, folder: str | Path, *, reason: str,
    ) -> str:
        """Explicitly demote an incomplete staging checkpoint for repair.

        This is deliberately absent from the normal lifecycle transition table
        and is callable only by an operator repair workflow.
        """
        if not _PAPER_NUMBER_RE.match(str(number or "")):
            raise ValueError(f"invalid paper_number: {number}")
        folder = Path(folder)
        if not folder.is_dir() or folder.name != str(number):
            raise ValueError("repair workspace must be the numeric paper_raw folder")
        with FileLock(str(self._lock_path)):
            data = self.load()
            items = data.setdefault("items", {})
            existing = items.get(number) or {}
            state = str(existing.get("state") or "")
            if state == LEDGER_RESERVED:
                return number
            assert_ledger_repair_transition(
                paper_number=number, current_state=state,
                target_state=LEDGER_RESERVED, reason=reason,
            )
            stored = resolve_stored_path(str(existing.get("folder_path") or ""))
            if stored.resolve() != folder.resolve():
                raise ValueError("repair workspace does not match ledger folder_path")
            patched = build_transition_patch(
                existing, number=number, target_state=LEDGER_RESERVED,
                folder=str(folder), folder_path=normalize_repo_path(folder),
                planned_paper_name=str(existing.get("planned_paper_name") or ""),
                now_iso=now_iso(),
            )
            patched["repair_reason"] = reason
            patched["repaired_at"] = now_iso()
            items[number] = patched
            self._save_unlocked(data)
            self.write_marker(
                folder, number, state=LEDGER_RESERVED,
                planned_paper_name=str(existing.get("planned_paper_name") or ""),
            )
            return number

    def activate_metadata_staged(
        self,
        number: str,
        final_folder: str | Path,
        paper_name: str = "",
    ) -> str:
        """Flip a ``metadata_staged`` number to ``active`` and repoint at the formal folder.

        Used by ``commit_paper_raw`` after ``os.replace`` installs the formal
        copy. The marker is rewritten with ``state="active"``. The number MUST
        already exist in the ledger as ``metadata_staged``; a ``reserved`` entry
        is REJECTED (historical ``reserved`` data must be repaired to
        ``metadata_staged`` first via an explicit repair command).

        Raises ``InvalidLedgerTransition`` if current state is not
        ``metadata_staged``.
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
            state = existing.get("state") or LEDGER_METADATA_STAGED

            if state == LEDGER_RESERVED:
                raise InvalidLedgerTransition(
                    number, state, LEDGER_ACTIVE,
                    reason="repair_required_reserved_final_mismatch: run repair to promote to metadata_staged first",
                )
            if state != LEDGER_METADATA_STAGED:
                raise InvalidLedgerTransition(
                    number, state, LEDGER_ACTIVE,
                    reason=f"commit only accepts metadata_staged, not {state}",
                )

            assert_ledger_transition(
                paper_number=number,
                current_state=LEDGER_METADATA_STAGED,
                target_state=LEDGER_ACTIVE,
            )
            if int(number) > int(str(data.get("max_number") or "0000000000000000")):
                data["max_number"] = number
            items[number] = build_transition_patch(
                existing,
                number=number,
                target_state=LEDGER_ACTIVE,
                folder=str(final_folder),
                folder_path=normalize_repo_path(final_folder),
                planned_paper_name=existing.get("planned_paper_name") or paper_name,
                paper_name=paper_name,
                now_iso=now_iso(),
            )
            self._save_unlocked(data)
            self.write_marker(
                final_folder,
                number,
                state=LEDGER_ACTIVE,
                planned_paper_name=paper_name or existing.get("planned_paper_name") or "",
            )
            return number

    def activate_metadata_staged_locked(
        self,
        number: str,
        final_folder: str | Path,
        paper_name: str = "",
    ) -> str:
        """Same as ``activate_metadata_staged`` but caller must hold the ledger lock.

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
        state = existing.get("state") or LEDGER_METADATA_STAGED

        if state == LEDGER_RESERVED:
            raise InvalidLedgerTransition(
                number, state, LEDGER_ACTIVE,
                reason="repair_required_reserved_final_mismatch: run repair to promote to metadata_staged first",
            )
        if state != LEDGER_METADATA_STAGED:
            raise InvalidLedgerTransition(
                number, state, LEDGER_ACTIVE,
                reason=f"commit only accepts metadata_staged, not {state}",
            )

        assert_ledger_transition(
            paper_number=number,
            current_state=LEDGER_METADATA_STAGED,
            target_state=LEDGER_ACTIVE,
        )
        if int(number) > int(str(data.get("max_number") or "0000000000000000")):
            data["max_number"] = number
        items[number] = build_transition_patch(
            existing,
            number=number,
            target_state=LEDGER_ACTIVE,
            folder=str(final_folder),
            folder_path=normalize_repo_path(final_folder),
            planned_paper_name=existing.get("planned_paper_name") or paper_name,
            paper_name=paper_name,
            now_iso=now_iso(),
        )
        self._save_unlocked(data)
        self.write_marker(
            final_folder,
            number,
            state=LEDGER_ACTIVE,
            planned_paper_name=paper_name or existing.get("planned_paper_name") or "",
        )
        return number

    def rollback_active_to_metadata_staged(
        self,
        number: str,
        raw_folder: str | Path,
        *,
        planned_paper_name: str = "",
    ) -> str:
        """Rollback a formal-library active number to ``metadata_staged``.

        Used by the explicit formal-library rollback tool. The metadata, source
        record, receipt, and manifest do NOT disappear when a paper is rolled
        back — only the formal install is removed, so the correct state is
        ``metadata_staged`` (not ``reserved``).

        Preserves the monotonic ledger state (including max_number and existing
        timestamps) while repointing the item at ``data/paper_raw/<paper_number>``.
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
            state = existing.get("state") or LEDGER_ACTIVE
            if state != LEDGER_ACTIVE:
                raise InvalidLedgerTransition(
                    number, state, LEDGER_METADATA_STAGED,
                    reason="rollback only applies to active entries",
                )
            assert_ledger_transition(
                paper_number=number,
                current_state=LEDGER_ACTIVE,
                target_state=LEDGER_METADATA_STAGED,
                reason="explicit_rollback",
            )
            planned = planned_paper_name or existing.get("planned_paper_name") or existing.get("paper_name") or ""
            items[number] = build_transition_patch(
                existing,
                number=number,
                target_state=LEDGER_METADATA_STAGED,
                folder=str(raw_folder),
                folder_path=normalize_repo_path(raw_folder),
                planned_paper_name=planned,
                now_iso=now_iso(),
            )
            self._save_unlocked(data)
            self.write_marker(raw_folder, number, state=LEDGER_METADATA_STAGED, planned_paper_name=planned)
            # The public rollback API may be used outside the transaction
            # coordinator by recovery tooling.  If a publication sidecar is
            # already present, keep its active set synchronized immediately;
            # the transaction coordinator performs the same publication under
            # its ranked papers/index locks.
            try:
                from src.discovery.formal_publication import (
                    publish_formal_publication_state,
                    publication_state_path,
                )
                papers_root = raw_folder.parent.parent / "papers"
                if publication_state_path(papers_root).is_file():
                    publish_formal_publication_state(
                        papers_dir=papers_root,
                        ledger_items=data.get("items") or {},
                    )
            except (OSError, ValueError):
                # A failed publication leaves discovery fail-closed and is
                # surfaced by the next Registry audit/recovery pass.
                pass
            return number

    def rollback_active_to_metadata_staged_locked(
        self,
        number: str,
        raw_folder: str | Path,
        *,
        planned_paper_name: str = "",
    ) -> str:
        """Same as ``rollback_active_to_metadata_staged`` but caller holds the lock.

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
        state = existing.get("state") or LEDGER_ACTIVE
        if state != LEDGER_ACTIVE:
            raise InvalidLedgerTransition(
                number, state, LEDGER_METADATA_STAGED,
                reason="rollback only applies to active entries",
            )
        assert_ledger_transition(
            paper_number=number,
            current_state=LEDGER_ACTIVE,
            target_state=LEDGER_METADATA_STAGED,
            reason="explicit_rollback",
        )
        planned = planned_paper_name or existing.get("planned_paper_name") or existing.get("paper_name") or ""
        items[number] = build_transition_patch(
            existing,
            number=number,
            target_state=LEDGER_METADATA_STAGED,
            folder=str(raw_folder),
            folder_path=normalize_repo_path(raw_folder),
            planned_paper_name=planned,
            now_iso=now_iso(),
        )
        self._save_unlocked(data)
        self.write_marker(raw_folder, number, state=LEDGER_METADATA_STAGED, planned_paper_name=planned)
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
