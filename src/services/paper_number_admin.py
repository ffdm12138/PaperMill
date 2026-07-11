"""Admin-only paper_number audit and reset/compact operations.

Normal ingest never recycles paper_numbers. This module is only for explicit
maintenance after the formal library has been rolled back or emptied.
"""
from __future__ import annotations

import json
import re
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

from filelock import FileLock

from config.settings import PAPER_NUMBER_LEDGER_PATH, PAPER_RAW_DIR, PAPERS_DIR
from src.path_utils import normalize_repo_path, resolve_stored_path
from src.services.ingest_duplicate_guard import is_paper_raw_workspace
from src.services.ingest_ids import PAPER_NUMBER_RE
from src.library.paper_number_ledger import PaperNumberLedger, now_iso
from src.utils.atomic_io import atomic_write_json


TEXT_SUFFIXES = {".json", ".md", ".txt", ".number"}
MARKER_SUFFIX = ".paper.number"
TOKEN_RE = re.compile(r"\d{16}")
# A standalone 16-digit paper_number token (not part of a longer digit run).
_STANDALONE_16DIGIT_RE = re.compile(r"(?<!\d)\d{16}(?!\d)")
# A hex hash run (md5=32, sha256=64) — 16-digit substrings inside these are NOT
# paper_numbers, they are hash fragments and must not be reported as stale refs.
_HEX_RUN_RE = re.compile(r"[0-9a-fA-F]{32,}")


def _find_stale_paper_number_tokens(text: str, current: str) -> list[str]:
    """Return 16-digit tokens in ``text`` that are not ``current`` and are not
    fragments of a longer hex hash (md5/sha256).

    The bare ``TOKEN_RE`` matches any 16-digit run, which false-positives on
    sha256/md5 hash fragments and on 17+ digit runs. A real stale paper_number
    reference is a standalone 16-digit token (surrounded by non-digits) that is
    not embedded in a 32+ char hex string.
    """
    hex_runs = [m.group() for m in _HEX_RUN_RE.finditer(text)]
    tokens: set[str] = set()
    for m in _STANDALONE_16DIGIT_RE.finditer(text):
        tok = m.group()
        if tok == current:
            continue
        if any(tok in run for run in hex_runs):
            continue
        tokens.add(tok)
    return sorted(tokens)
# A standalone 16-digit paper_number token (not part of a longer digit run).
_STANDALONE_16DIGIT_RE = re.compile(r"(?<!\d)\d{16}(?!\d)")
_METADATA_FINGERPRINT_PLACEHOLDER = "PAPER_NUMBER_TOKEN"


class MetadataFingerprintMismatch(RuntimeError):
    """Raised when compact rewrites a metadata file's bibliographic content.

    The normalized fingerprint ignores ``paper_number``/``paper_raw_id`` and
    replaces every standalone 16-digit token with a placeholder, so a mismatch
    means the rewrite touched a bibliographic field (DOI/authors/title/...),
    which is a bug. compact must roll back and never write the new ledger.
    """

    def __init__(self, mismatches: list[dict[str, Any]]):
        self.mismatches = mismatches
        details = "; ".join(
            f"{m['old_number']}: {m['diff_paths']}" for m in mismatches
        )
        super().__init__(f"metadata fingerprint mismatch after compact: {details}")


def replace_16_digit_paper_numbers_with_placeholder(value: Any) -> Any:
    """Recursively replace standalone 16-digit paper_number tokens with a placeholder."""
    if isinstance(value, str):
        return _STANDALONE_16DIGIT_RE.sub(_METADATA_FINGERPRINT_PLACEHOLDER, value)
    if isinstance(value, list):
        return [replace_16_digit_paper_numbers_with_placeholder(v) for v in value]
    if isinstance(value, dict):
        return {k: replace_16_digit_paper_numbers_with_placeholder(v) for k, v in value.items()}
    return value


def normalized_metadata_for_fingerprint(metadata: dict) -> dict:
    """Strip identity fields and 16-digit tokens so fingerprinting only sees
    bibliographic content. ``paper_number``/``paper_raw_id`` and internal
    16-digit path references legitimately change during compact; everything else
    (DOI, authors, year, title, container, identifiers, links, metadata_match,
    ...) must be byte-stable."""
    clone = deepcopy(metadata)
    clone.pop("paper_number", None)
    clone.pop("paper_raw_id", None)
    return replace_16_digit_paper_numbers_with_placeholder(clone)


def metadata_fingerprint(metadata: dict) -> str:
    """sha256 over canonical JSON of the normalized metadata."""
    import hashlib
    normalized = normalized_metadata_for_fingerprint(metadata)
    canonical = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return {} if default is None else default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default


def _write_json(path: Path, data: dict) -> None:
    atomic_write_json(path, data, indent=2)


def _replace_token(text: str, old: str, new: str) -> str:
    return re.sub(rf"(?<!\d){re.escape(old)}(?!\d)", new, text)


def _replace_in_obj(value: Any, old: str, new: str) -> Any:
    if isinstance(value, str):
        return _replace_token(value, old, new)
    if isinstance(value, list):
        return [_replace_in_obj(item, old, new) for item in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"paper_number", "paper_raw_id"}:
                out[key] = new
            else:
                out[key] = _replace_in_obj(item, old, new)
        return out
    return value


def _is_text_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in TEXT_SUFFIXES


def _safe_year(metadata: dict) -> int | None:
    value = metadata.get("year") if isinstance(metadata, dict) else None
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _has_real_raw_assets(folder: Path) -> bool:
    patterns = (
        "*.paper.number",
        "*.metadata.json",
        "*.catalog.json",
        "*.asset_manifest.json",
        "*.conversion.json",
        "*.formalization.json",
        "*.pdf",
        "*.md",
    )
    return any(any(folder.glob(pattern)) for pattern in patterns)


def _is_empty_invalid_folder(folder: Path) -> bool:
    if _has_real_raw_assets(folder):
        return False
    allowed = {".import_status.json", ".DS_Store", "Thumbs.db"}
    for child in folder.iterdir():
        if child.is_dir():
            return False
        if child.name not in allowed:
            return False
    return True


def rewrite_workspace_numbers(folder: Path, old: str, new: str, *, file_prefix_mode: str) -> list[str]:
    """Rewrite one workspace from ``old`` to ``new`` paper_number.

    ``file_prefix_mode`` is descriptive for reports: ``numbered`` means the
    surrounding workspace folder may also be renamed by the caller; ``named``
    means only files/content are changed.
    """
    if not PAPER_NUMBER_RE.match(old) or not PAPER_NUMBER_RE.match(new):
        raise ValueError("old/new paper_number must be 16 digits")
    changes: list[str] = []

    # Rename files/directories that contain the complete old token. Process
    # deeper paths first so a parent rename does not invalidate child paths.
    paths = sorted(
        [p for p in folder.rglob("*") if p.name and re.search(rf"(?<!\d){re.escape(old)}(?!\d)", p.name)],
        key=lambda p: len(p.parts),
        reverse=True,
    )
    for path in paths:
        target = path.with_name(_replace_token(path.name, old, new))
        if target == path:
            continue
        if target.exists():
            raise FileExistsError(f"renumber target already exists: {target}")
        path.rename(target)
        changes.append(f"rename:{normalize_repo_path(path)}->{normalize_repo_path(target)}")

    for path in sorted(p for p in folder.rglob("*") if _is_text_file(p)):
        if path.suffix.lower() == ".json":
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                raw = path.read_text(encoding="utf-8")
                updated = _replace_token(raw, old, new)
                if updated != raw:
                    path.write_text(updated, encoding="utf-8")
                    changes.append(f"text:{normalize_repo_path(path)}")
                continue
            updated = _replace_in_obj(data, old, new)
            if path.name.endswith(MARKER_SUFFIX) and isinstance(updated, dict):
                updated["paper_number"] = new
                updated["folder_name"] = folder.name
            if updated != data:
                _write_json(path, updated)
                changes.append(f"json:{normalize_repo_path(path)}")
            continue

        raw = path.read_text(encoding="utf-8")
        updated = _replace_token(raw, old, new)
        if path.name.endswith(MARKER_SUFFIX):
            try:
                marker_data = json.loads(updated)
            except Exception:
                marker_data = None
            if isinstance(marker_data, dict):
                marker_data["paper_number"] = new
                marker_data["folder_name"] = folder.name
                updated = json.dumps(marker_data, ensure_ascii=False, indent=2)
        if updated != raw:
            path.write_text(updated, encoding="utf-8")
            changes.append(f"text:{normalize_repo_path(path)}")

    # Ensure marker content exists and is canonical after any fallback text pass.
    markers = sorted(folder.glob("*.paper.number"))
    if len(markers) != 1:
        raise RuntimeError(f"expected one marker after rewrite in {folder}, found {len(markers)}")
    marker_data = _read_json(markers[0], {})
    if not isinstance(marker_data, dict):
        raise RuntimeError(f"marker is not JSON after rewrite: {markers[0]}")
    marker_data["paper_number"] = new
    marker_data["folder_name"] = folder.name
    marker_data.setdefault("state", "reserved")
    _write_json(markers[0], marker_data)
    changes.append(f"marker:{normalize_repo_path(markers[0])}:{file_prefix_mode}")
    return changes


class PaperNumberAdminService:
    def __init__(
        self,
        *,
        paper_raw_dir: str | Path = PAPER_RAW_DIR,
        papers_dir: str | Path = PAPERS_DIR,
        ledger_path: str | Path = PAPER_NUMBER_LEDGER_PATH,
        transactions_dir: str | Path | None = None,
    ):
        self.paper_raw_dir = Path(paper_raw_dir)
        self.papers_dir = Path(papers_dir)
        self.ledger = PaperNumberLedger(ledger_path)
        if transactions_dir is None:
            transactions_dir = self.paper_raw_dir.parent / "transactions"
        self.transactions_dir = Path(transactions_dir)

    def _new_transaction_dir(self) -> Path:
        base = self.transactions_dir / f"paper_number_reset_{now_iso().replace(':', '').replace('-', '')}"
        candidate = base
        i = 1
        while candidate.exists():
            i += 1
            candidate = Path(f"{base}_{i}")
        candidate.mkdir(parents=True, exist_ok=False)
        return candidate

    def _papers_count(self) -> int:
        if not self.papers_dir.exists():
            return 0
        return sum(1 for p in self.papers_dir.iterdir() if p.is_dir() and not p.name.startswith("."))

    def _workspace_report(self, folder: Path) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        markers = sorted(folder.glob("*.paper.number"))
        marker_number = ""
        marker_data: dict[str, Any] = {}
        if len(markers) != 1:
            errors.append(f"expected exactly one .paper.number marker, found {len(markers)}")
        else:
            marker_number = PaperNumberLedger.parse_marker_number(markers[0]) or ""
            if not marker_number:
                errors.append(f"invalid marker filename: {markers[0].name}")
            try:
                loaded = json.loads(markers[0].read_text(encoding="utf-8"))
            except Exception as exc:
                errors.append(f"marker content is not JSON: {exc}")
            else:
                if not isinstance(loaded, dict):
                    errors.append("marker content is not a JSON object")
                else:
                    marker_data = loaded
                    if marker_number and loaded.get("paper_number") != marker_number:
                        errors.append("marker filename/content paper_number mismatch")

        metadata_paths = sorted(folder.glob("*.metadata.json"))
        metadata = _read_json(metadata_paths[0], {}) if metadata_paths else {}
        if metadata_paths and not isinstance(metadata, dict):
            errors.append("metadata is not a JSON object")
            metadata = {}
        if marker_number and isinstance(metadata, dict) and metadata:
            if metadata.get("paper_number") != marker_number:
                errors.append("metadata.paper_number mismatch")
            if metadata.get("paper_raw_id") != marker_number:
                errors.append("metadata.paper_raw_id mismatch")
        if marker_number and PAPER_NUMBER_RE.match(folder.name) and folder.name != marker_number:
            errors.append("16-digit folder name does not match marker")

        ledger_item = (self.ledger.load().get("items") or {}).get(marker_number) if marker_number else None
        state = ""
        if ledger_item:
            state = str((ledger_item or {}).get("state") or "active")
        year = _safe_year(metadata if isinstance(metadata, dict) else {})
        status = str((_read_json(folder / ".import_status.json", {}) or {}).get("status") or "")
        return {
            "folder": str(folder),
            "folder_name": folder.name,
            "paper_number": marker_number,
            "marker_path": str(markers[0]) if markers else "",
            "metadata_paths": [str(p) for p in metadata_paths],
            "year": year,
            "status": status,
            "state": state,
            "marker": marker_data,
            "ledger_item": deepcopy(ledger_item) if ledger_item else None,
            "errors": errors,
            "warnings": warnings,
            "valid": not errors and bool(marker_number),
            "numbered_folder": bool(PAPER_NUMBER_RE.match(folder.name)),
        }

    def _collect_raw(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        valid: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        if not self.paper_raw_dir.exists():
            return valid, invalid
        for folder in sorted(p for p in self.paper_raw_dir.iterdir() if p.is_dir()):
            if folder.name == "quarantine" or folder.name.startswith("."):
                continue
            if not is_paper_raw_workspace(folder):
                continue
            info = self._workspace_report(folder)
            if info["valid"]:
                valid.append(info)
            else:
                info["empty_invalid"] = _is_empty_invalid_folder(folder)
                invalid.append(info)
        return valid, invalid

    def _orphan_classification_report(self, ledger_data: dict) -> dict[str, Any]:
        classifications: list[dict[str, Any]] = []
        if self.paper_raw_dir.exists():
            for folder in sorted(p for p in self.paper_raw_dir.iterdir() if p.is_dir()):
                if folder.name == "quarantine" or folder.name.startswith("."):
                    continue
                if PAPER_NUMBER_RE.match(folder.name) or is_paper_raw_workspace(folder):
                    classifications.append(PaperNumberLedger.classify_paper_raw_workspace(folder, folder.name))
        items = ledger_data.get("items") or {}
        ledger_allocating_missing_folder: list[dict[str, Any]] = []
        ledger_items_missing_folder: list[dict[str, Any]] = []
        for number, item in sorted(items.items()):
            stored = str((item or {}).get("folder_path") or "")
            folder = resolve_stored_path(stored) if stored else self.paper_raw_dir / number
            if folder.exists():
                continue
            entry = {
                "paper_number": number,
                "state": str((item or {}).get("state") or ""),
                "folder_path": stored,
                "classification": "missing_folder_for_ledger_item",
            }
            ledger_items_missing_folder.append(entry)
            if entry["state"] == "allocating":
                ledger_allocating_missing_folder.append(entry)
        return {
            "workspace_classifications": classifications,
            "empty_orphan_dirs": [c for c in classifications if c["classification"] == "empty_orphan_dir"],
            "marker_only_reserved": [c for c in classifications if c["classification"] == "marker_only_reserved"],
            "metadata_only_workspaces": [c for c in classifications if c["classification"] == "metadata_only_workspace"],
            "unknown_nonempty": [c for c in classifications if c["classification"] == "unknown_nonempty"],
            "ledger_allocating_missing_folder": ledger_allocating_missing_folder,
            "ledger_items_missing_folder": ledger_items_missing_folder,
        }

    def audit(
        self,
        *,
        strict: bool = False,
        expect_count: int | None = None,
        detect_orphans: bool = False,
    ) -> dict[str, Any]:
        ledger_data = self.ledger.load()
        valid, invalid = self._collect_raw()
        paper_numbers: dict[str, list[str]] = {}
        for info in valid:
            paper_numbers.setdefault(info["paper_number"], []).append(info["folder"])

        duplicated = [
            {"paper_number": number, "folders": folders}
            for number, folders in sorted(paper_numbers.items())
            if len(folders) > 1
        ]
        items = ledger_data.get("items") or {}
        raw_numbers = set(paper_numbers)
        ledger_numbers = set(items)
        missing_ledger = sorted(raw_numbers - ledger_numbers)
        orphan_ledger: list[dict[str, Any]] = []
        state_counts: dict[str, int] = {}
        for number, item in sorted(items.items()):
            state = str((item or {}).get("state") or "active")
            state_counts[state] = state_counts.get(state, 0) + 1
            stored = str((item or {}).get("folder_path") or "")
            folder = resolve_stored_path(stored) if stored else Path("")
            if number not in raw_numbers:
                if state == "active":
                    formal = folder if folder else self.papers_dir / str((item or {}).get("folder_name") or "")
                    if not formal.exists():
                        orphan_ledger.append({"paper_number": number, "state": state, "folder_path": stored})
                elif state.startswith("quarantined"):
                    continue
                else:
                    if not folder.exists():
                        orphan_ledger.append({"paper_number": number, "state": state, "folder_path": stored})

        stale_refs: list[dict[str, Any]] = []
        # A "stale" token is only meaningful if it is an actual paper_number
        # (in the active set or ledger). This excludes ISSN/DOI/hash 16-digit
        # false positives that would otherwise block compact on noise.
        known_paper_numbers = raw_numbers | ledger_numbers
        for info in valid:
            current = info["paper_number"]
            folder = Path(info["folder"])
            for path in sorted(p for p in folder.rglob("*") if _is_text_file(p)):
                try:
                    text = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                tokens = [t for t in _find_stale_paper_number_tokens(text, current)
                          if t in known_paper_numbers]
                if tokens:
                    stale_refs.append({
                        "folder": str(folder),
                        "file": str(path),
                        "paper_number": current,
                        "tokens": tokens,
                    })

        # .paper pollution: any active workspace folder name or child file whose
        # name ends in ``.paper`` but NOT ``.paper.number`` (the classic
        # Path.stem marker-parsing bug). Always blocking — it corrupts identity.
        paper_pollution: list[dict[str, Any]] = []
        for info in valid + invalid:
            folder = Path(info["folder"])
            for name in [folder.name] + [p.name for p in folder.iterdir() if p.is_file()]:
                if name.endswith(".paper") and not name.endswith(".paper.number"):
                    paper_pollution.append({"folder": str(folder), "polluted_name": name})

        folder_marker_mismatches = [
            info for info in valid + invalid if any("folder" in err for err in info.get("errors", []))
        ]
        marker_content_mismatches = [
            info for info in valid + invalid if any("marker" in err for err in info.get("errors", []))
        ]
        metadata_number_mismatches = [
            info for info in valid + invalid if any("metadata.paper_number" in err for err in info.get("errors", []))
        ]
        metadata_raw_id_mismatches = [
            info for info in valid + invalid if any("metadata.paper_raw_id" in err for err in info.get("errors", []))
        ]
        active_with_empty_papers = [
            number for number, item in sorted(items.items())
            if str((item or {}).get("state") or "active") == "active" and self._papers_count() == 0
        ]
        active_state_count = state_counts.get("active", 0)
        sorted_valid_numbers = sorted(raw_numbers)
        expected_contiguous = [f"{i:016d}" for i in range(1, len(valid) + 1)] if valid else []
        numbers_contiguous = sorted_valid_numbers == expected_contiguous
        max_number = str(ledger_data.get("max_number") or "0000000000000000")
        # expect_count gates (only enforced when expect_count is set): the active
        # count, ledger item count, ledger max_number, and number contiguity must
        # all agree on exactly N workspaces numbered 1..N with no active state.
        expect_count_mismatches: list[str] = []
        if expect_count is not None:
            if len(valid) != expect_count:
                expect_count_mismatches.append(f"active_workspace_count={len(valid)} != expect_count={expect_count}")
            if len(items) != expect_count:
                expect_count_mismatches.append(f"ledger_item_count={len(items)} != expect_count={expect_count}")
            expected_max = f"{expect_count:016d}"
            if max_number != expected_max:
                expect_count_mismatches.append(f"ledger_max_number={max_number} != expect_count={expected_max}")
            if not numbers_contiguous:
                expect_count_mismatches.append("paper_numbers are not contiguous 1..N")
            if active_state_count:
                expect_count_mismatches.append(f"ledger has {active_state_count} active-state item(s); expected all reserved")

        orphan_report = self._orphan_classification_report(ledger_data) if detect_orphans else {}
        orphan_blocking = bool(
            detect_orphans and (
                orphan_report.get("empty_orphan_dirs")
                or orphan_report.get("marker_only_reserved")
                or orphan_report.get("ledger_allocating_missing_folder")
                or orphan_report.get("ledger_items_missing_folder")
            )
        )
        blocking = bool(
            invalid
            or duplicated
            or missing_ledger
            or orphan_ledger
            or stale_refs
            or folder_marker_mismatches
            or marker_content_mismatches
            or metadata_number_mismatches
            or metadata_raw_id_mismatches
            or active_with_empty_papers
            or paper_pollution
            or expect_count_mismatches
            or orphan_blocking
        )
        report = {
            "strict": strict,
            "ok": not blocking,
            "summary": {
                "papers_dir_count": self._papers_count(),
                "paper_raw_valid_count": len(valid),
                "paper_raw_invalid_count": len(invalid),
                "ledger_item_count": len(items),
                "ledger_state_counts": state_counts,
                "ledger_active_state_count": active_state_count,
                "numbers_contiguous": numbers_contiguous,
                "paper_number_pollution_count": len(paper_pollution),
                "expect_count": expect_count,
                "expect_count_mismatches": expect_count_mismatches,
                "blocking": blocking,
            },
            "ledger": {
                "path": str(self.ledger.path),
                "schema_version": ledger_data.get("schema_version"),
                "max_number": ledger_data.get("max_number"),
            },
            "raw_workspaces": valid,
            "invalid_workspaces": invalid,
            "folder_marker_mismatches": folder_marker_mismatches,
            "marker_content_mismatches": marker_content_mismatches,
            "metadata_paper_number_mismatches": metadata_number_mismatches,
            "metadata_paper_raw_id_mismatches": metadata_raw_id_mismatches,
            "duplicated_paper_numbers": duplicated,
            "missing_ledger_items": missing_ledger,
            "orphan_ledger_items": orphan_ledger,
            "active_ledger_items_with_empty_papers": active_with_empty_papers,
            "stale_16_digit_refs": stale_refs,
            "paper_number_pollution": paper_pollution,
        }
        if detect_orphans:
            report.update(orphan_report)
        return report

    def fix_empty_orphans(self, *, apply: bool = False, reason: str = "") -> dict[str, Any]:
        tx_dir = self._new_transaction_dir()
        ledger_before = self.ledger.load()
        audit_report = self.audit(strict=False, detect_orphans=True)
        targets = [Path(item["folder"]) for item in audit_report.get("empty_orphan_dirs") or []]
        mapping = {"old_to_new": {}, "items": []}
        self._write_transaction_common(tx_dir, audit_report, mapping, ledger_before)
        report: dict[str, Any] = {
            "applied": False,
            "transaction_dir": str(tx_dir),
            "operation": "fix_empty_orphans",
            "reason": reason,
            "targets": [str(path) for path in targets],
            "errors": [],
        }
        if apply and not reason:
            report["errors"].append("--reason is required with --apply")
        if apply and not report["errors"]:
            with FileLock(str(self.paper_raw_dir / ".paper_raw_write.lock")):
                for target in targets:
                    if not target.exists() or not target.is_dir():
                        continue
                    if any(target.iterdir()):
                        report["errors"].append(f"not empty, refused: {target}")
                        continue
                    target.rmdir()
                if not report["errors"]:
                    report["applied"] = True
                    report["post_audit"] = self.audit(strict=False, detect_orphans=True)
        _write_json(tx_dir / "renumber_report.json", report)
        return report

    def _write_transaction_common(self, tx_dir: Path, audit_report: dict, mapping: dict, ledger_before: dict) -> None:
        _write_json(tx_dir / "preflight_report.json", audit_report)
        _write_json(tx_dir / "mapping.json", mapping)
        _write_json(tx_dir / "paper_number_ledger.before.json", ledger_before)
        _write_json(tx_dir / "rollback_manifest.json", {"renames": []})

    def reset_empty(
        self,
        *,
        apply: bool = False,
        reason: str = "",
        purge_empty_invalid: bool = False,
    ) -> dict[str, Any]:
        tx_dir = self._new_transaction_dir()
        ledger_before = self.ledger.load()
        audit_report = self.audit(strict=False)
        valid = audit_report["raw_workspaces"]
        invalid = audit_report["invalid_workspaces"]
        errors: list[str] = []
        try:
            PaperNumberLedger.assert_papers_empty(self.papers_dir)
        except RuntimeError as exc:
            errors.append(str(exc))
        if valid:
            errors.append("paper_raw contains valid workspaces; use --compact-paper-raw instead")
        purge_targets = [Path(item["folder"]) for item in invalid if item.get("empty_invalid")]
        blocking_invalid = [item for item in invalid if not item.get("empty_invalid")]
        if blocking_invalid:
            errors.append("paper_raw contains invalid non-empty workspaces")
        if invalid and not purge_empty_invalid:
            errors.append("paper_raw contains invalid workspaces; use --purge-empty-invalid for empty corpses")
        mapping = {"old_to_new": {}, "items": []}
        self._write_transaction_common(tx_dir, audit_report, mapping, ledger_before)
        report = {
            "applied": False,
            "transaction_dir": str(tx_dir),
            "operation": "reset_empty",
            "errors": errors,
            "purge_targets": [str(p) for p in purge_targets],
        }
        if errors:
            _write_json(tx_dir / "renumber_report.json", report)
            return report
        if apply:
            with FileLock(str(self.ledger._lock_path)):
                if purge_empty_invalid:
                    for target in purge_targets:
                        shutil.rmtree(target)
                after = self.ledger.empty_data()
                history = list(ledger_before.get("reset_history") or [])
                if reason:
                    history.append({"reset_at": now_iso(), "reason": reason})
                if history:
                    after["reset_history"] = history
                self.ledger._save_unlocked(after)
            _write_json(tx_dir / "paper_number_ledger.after.json", after)
            report["applied"] = True
            report["post_audit"] = self.audit(strict=True)
        else:
            _write_json(tx_dir / "paper_number_ledger.after.json", ledger_before)
        _write_json(tx_dir / "renumber_report.json", report)
        return report

    def _build_mapping(self, workspaces: list[dict[str, Any]], *, sort: str, reason: str) -> dict[str, Any]:
        if sort == "year":
            ordered = sorted(
                workspaces,
                key=lambda item: (
                    item["year"] is None,
                    item["year"] if item["year"] is not None else 999999,
                    int(item["paper_number"]),
                ),
            )
        else:
            ordered = sorted(workspaces, key=lambda item: int(item["paper_number"]))
        old_to_new: dict[str, str] = {}
        items: list[dict[str, Any]] = []
        for index, info in enumerate(ordered, start=1):
            old = info["paper_number"]
            new = f"{index:016d}"
            old_to_new[old] = new
            folder_before = Path(info["folder"])
            folder_after = self.paper_raw_dir / new if info["numbered_folder"] else folder_before
            items.append({
                "old_number": old,
                "new_number": new,
                "folder_before": str(folder_before),
                "folder_after": str(folder_after),
                "year": info["year"],
                "status_before": info["status"],
                "state_before": info["state"] or "reserved",
                "numbered_folder": info["numbered_folder"],
                "created_at": ((info.get("ledger_item") or {}).get("created_at") or now_iso()),
                "planned_paper_id": ((info.get("ledger_item") or {}).get("planned_paper_id") or ""),
                "renumber_reason": reason,
            })
        return {"old_to_new": old_to_new, "items": items}

    def _new_ledger_from_mapping(self, mapping: dict[str, Any], *, reason: str) -> dict[str, Any]:
        items: dict[str, Any] = {}
        now = now_iso()
        for item in mapping["items"]:
            number = item["new_number"]
            folder_after = Path(item["folder_after"])
            items[number] = {
                "folder_name": folder_after.name,
                "folder_path": normalize_repo_path(folder_after),
                "planned_paper_id": item.get("planned_paper_id") or "",
                "state": "reserved",
                "created_at": item.get("created_at") or now,
                "renumbered_from": item["old_number"],
                "renumbered_at": now,
                "renumber_reason": reason,
            }
        max_number = f"{len(items):016d}" if items else "0000000000000000"
        return {"schema_version": "1.0", "max_number": max_number, "items": items}

    def _record_rename(self, rollback: dict[str, Any], src: Path, dst: Path) -> None:
        rollback.setdefault("renames", []).append({"from": str(src), "to": str(dst)})

    def _rollback_renames(self, rollback: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        for item in reversed(rollback.get("renames") or []):
            src = Path(item["to"])
            dst = Path(item["from"])
            try:
                if src.exists() and not dst.exists():
                    src.rename(dst)
            except Exception as exc:
                errors.append(f"rollback failed {src} -> {dst}: {exc}")
        return errors

    # ---- metadata fingerprint protection for compact ----

    def _collect_metadata_fingerprints(
        self, mapping: dict[str, Any]
    ) -> tuple[dict[str, str], dict[str, dict]]:
        """Return ``({old_number: fingerprint}, {old_number: metadata_dict})``.

        Reads each workspace's ``.metadata.json`` from ``folder_before`` before
        any rewrite. The backup dict enables emergency restore if the rewrite
        corrupts a bibliographic field.
        """
        from src.services.ingest_duplicate_guard import read_best_metadata_json

        fingerprints: dict[str, str] = {}
        backups: dict[str, dict] = {}
        for item in mapping["items"]:
            old = item["old_number"]
            folder = Path(item["folder_before"])
            metadata = read_best_metadata_json(folder)
            fingerprints[old] = metadata_fingerprint(metadata)
            backups[old] = deepcopy(metadata)
        return fingerprints, backups

    def _verify_metadata_fingerprints(
        self, mapping: dict[str, Any], fingerprints_before: dict[str, str],
        metadata_backups: dict[str, dict],
    ) -> list[dict[str, Any]]:
        """Compare post-rewrite fingerprints to pre-rewrite. Returns mismatches."""
        from src.services.ingest_duplicate_guard import read_best_metadata_json

        mismatches: list[dict[str, Any]] = []
        for item in mapping["items"]:
            old = item["old_number"]
            folder = Path(item["folder_after"])
            metadata = read_best_metadata_json(folder)
            after = metadata_fingerprint(metadata)
            before = fingerprints_before.get(old, "")
            if before != after:
                diff_paths = self._metadata_diff_paths(metadata_backups.get(old, {}), metadata)
                mismatches.append({
                    "old_number": old,
                    "new_number": item["new_number"],
                    "folder_after": str(folder),
                    "fingerprint_before": before,
                    "fingerprint_after": after,
                    "diff_paths": diff_paths,
                })
        return mismatches

    def _metadata_diff_paths(self, before: dict, after: dict) -> list[str]:
        """Top-level keys whose normalized values differ, to point at the
        corrupted field. (Deep recursive diff is overkill; the fingerprint
        already proves a mismatch exists.)"""
        diff: list[str] = []
        norm_before = normalized_metadata_for_fingerprint(before)
        norm_after = normalized_metadata_for_fingerprint(after)
        keys = sorted(set(norm_before) | set(norm_after))
        for key in keys:
            if norm_before.get(key) != norm_after.get(key):
                diff.append(key)
        return diff

    def _restore_metadata_backups(
        self, mapping: dict[str, Any], backups: dict[str, dict]
    ) -> list[str]:
        """Write the original metadata dict back into each workspace's
        ``*.metadata.json`` (located by glob, since the file may have been
        renamed by the rewrite). Best-effort; errors are returned, not raised."""
        errors: list[str] = []
        for item in mapping["items"]:
            old = item["old_number"]
            folder = Path(item["folder_after"])
            if not folder.exists():
                continue
            metadata_files = sorted(folder.glob("*.metadata.json"))
            if not metadata_files:
                continue
            backup = backups.get(old)
            if not isinstance(backup, dict):
                continue
            try:
                _write_json(metadata_files[0], backup)
            except Exception as exc:
                errors.append(f"metadata restore failed for {metadata_files[0]}: {exc}")
        return errors

    def compact_paper_raw(
        self,
        *,
        apply: bool = False,
        reason: str = "",
        sort: str = "old-number",
        purge_empty_invalid: bool = False,
        protect_metadata: bool = False,
    ) -> dict[str, Any]:
        tx_dir = self._new_transaction_dir()
        ledger_before = self.ledger.load()
        audit_report = self.audit(strict=False)
        errors: list[str] = []
        try:
            PaperNumberLedger.assert_papers_empty(self.papers_dir)
        except RuntimeError as exc:
            errors.append(str(exc))
        if sort not in {"old-number", "year"}:
            errors.append(f"unsupported sort: {sort}")
        active = audit_report.get("active_ledger_items_with_empty_papers") or []
        if active:
            errors.append("ledger contains active items while data/papers is empty")
        invalid = audit_report["invalid_workspaces"]
        purge_targets = [Path(item["folder"]) for item in invalid if item.get("empty_invalid")]
        blocking_invalid = [item for item in invalid if not item.get("empty_invalid")]
        if blocking_invalid:
            errors.append("paper_raw contains invalid non-empty workspaces")
        if invalid and not purge_empty_invalid:
            errors.append("paper_raw contains invalid workspaces; use --purge-empty-invalid for empty corpses")
        # ``protect_metadata`` compacts renumber EVERY workspace and rebuild the
        # ledger from scratch, so duplicate/missing/orphan paper_numbers (which
        # are the normal pre-compact safety gates) are expected when restoring
        # legacy/untitled workspaces whose markers collide with numbered ones.
        # They are resolved by the renumber + ledger rebuild. Real corruption
        # checks (invalid workspaces, stale 16-digit refs, active items with
        # empty papers) are still enforced.
        relax_preflight = (
            "duplicated_paper_numbers",
            "missing_ledger_items",
            "orphan_ledger_items",
        ) if protect_metadata else ()
        for key in (
            "duplicated_paper_numbers",
            "missing_ledger_items",
            "orphan_ledger_items",
            "stale_16_digit_refs",
        ):
            if key in relax_preflight:
                continue
            if audit_report.get(key):
                errors.append(f"audit blocking issue: {key}")
        mapping = self._build_mapping(audit_report["raw_workspaces"], sort=sort, reason=reason)
        self._write_transaction_common(tx_dir, audit_report, mapping, ledger_before)
        # Metadata fingerprint backup: capture the bibliographic fingerprint of
        # every workspace BEFORE any rewrite so compact can verify it only
        # changed paper_number/paper_raw_id + 16-digit tokens and roll back
        # otherwise. ``metadata_backups`` holds the original parsed metadata
        # for emergency restore on mismatch.
        fingerprints_before: dict[str, str] = {}
        metadata_backups: dict[str, dict] = {}
        if protect_metadata:
            fingerprints_before, metadata_backups = self._collect_metadata_fingerprints(mapping)
            _write_json(tx_dir / "metadata_fingerprints_before.json", fingerprints_before)
        rollback: dict[str, Any] = {"renames": []}
        report: dict[str, Any] = {
            "applied": False,
            "transaction_dir": str(tx_dir),
            "operation": "compact_paper_raw",
            "sort": sort,
            "errors": errors,
            "mapping_count": len(mapping["items"]),
            "metadata_protected": protect_metadata,
            "purge_targets": [str(p) for p in purge_targets],
            "changes": [],
        }
        if errors:
            _write_json(tx_dir / "renumber_report.json", report)
            return report
        if not apply:
            _write_json(tx_dir / "paper_number_ledger.after.json", self._new_ledger_from_mapping(mapping, reason=reason))
            _write_json(tx_dir / "renumber_report.json", report)
            return report

        try:
            with FileLock(str(self.ledger._lock_path)):
                if purge_empty_invalid:
                    for target in purge_targets:
                        shutil.rmtree(target)
                        report["changes"].append(f"purge:{normalize_repo_path(target)}")

                working_paths: dict[str, Path] = {}
                for item in mapping["items"]:
                    old = item["old_number"]
                    folder = Path(item["folder_before"])
                    if item["numbered_folder"] and old != item["new_number"]:
                        tmp = self.paper_raw_dir / f".renumber_tmp_{old}"
                        if tmp.exists():
                            raise FileExistsError(f"temporary renumber folder exists: {tmp}")
                        folder.rename(tmp)
                        self._record_rename(rollback, folder, tmp)
                        working_paths[old] = tmp
                    else:
                        working_paths[old] = folder

                _write_json(tx_dir / "rollback_manifest.json", rollback)

                for item in mapping["items"]:
                    old = item["old_number"]
                    new = item["new_number"]
                    folder = working_paths[old]
                    mode = "numbered" if item["numbered_folder"] else "named"
                    report["changes"].extend(rewrite_workspace_numbers(folder, old, new, file_prefix_mode=mode))

                for item in mapping["items"]:
                    old = item["old_number"]
                    new = item["new_number"]
                    folder = working_paths[old]
                    final = Path(item["folder_after"])
                    if folder != final:
                        if final.exists():
                            raise FileExistsError(f"final renumber folder exists: {final}")
                        folder.rename(final)
                        self._record_rename(rollback, folder, final)
                        report["changes"].append(f"folder:{normalize_repo_path(folder)}->{normalize_repo_path(final)}")
                    markers = sorted(final.glob("*.paper.number"))
                    if len(markers) != 1:
                        raise RuntimeError(f"expected one marker after final rename in {final}, found {len(markers)}")
                    marker_data = _read_json(markers[0], {})
                    if not isinstance(marker_data, dict):
                        raise RuntimeError(f"marker is not JSON after final rename: {markers[0]}")
                    marker_data["paper_number"] = new
                    marker_data["folder_name"] = final.name
                    marker_data.setdefault("state", "reserved")
                    _write_json(markers[0], marker_data)

                # Metadata protection gate: verify the rewrite only touched
                # paper_number/paper_raw_id + 16-digit tokens. Must pass BEFORE
                # the new ledger is written; on mismatch, restore metadata and
                # raise so the rename rollback fires and no ledger is saved.
                if protect_metadata:
                    mismatches = self._verify_metadata_fingerprints(mapping, fingerprints_before, metadata_backups)
                    if mismatches:
                        restore_errors = self._restore_metadata_backups(mapping, metadata_backups)
                        if restore_errors:
                            report["metadata_restore_errors"] = restore_errors
                        _write_json(tx_dir / "metadata_fingerprint_mismatches.json", mismatches)
                        raise MetadataFingerprintMismatch(mismatches)

                _write_json(tx_dir / "rollback_manifest.json", rollback)
                new_ledger = self._new_ledger_from_mapping(mapping, reason=reason)
                self.ledger._save_unlocked(new_ledger)
                _write_json(tx_dir / "paper_number_ledger.after.json", new_ledger)
                report["applied"] = True
        except Exception as exc:
            report["errors"].append(str(exc))
            report["rollback_errors"] = self._rollback_renames(rollback)
            _write_json(tx_dir / "rollback_manifest.json", rollback)
            _write_json(tx_dir / "renumber_report.json", report)
            return report

        post_audit = self.audit(strict=True)
        report["post_audit"] = post_audit
        if not post_audit.get("ok"):
            report["errors"].append("post-compact strict audit failed")
        _write_json(tx_dir / "renumber_report.json", report)
        return report
