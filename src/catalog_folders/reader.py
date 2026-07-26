from __future__ import annotations

import json
import re
from pathlib import Path

from src.catalog_folders.link_backend import inspect_paper_link


SYSTEM_DIRS = {"all", "_pending", ".state"}

_PAPER_NUMBER_RE = re.compile(r"^\d{16}$")


class CatalogFolderReader:
    def __init__(self, *, root: Path, papers_dir: Path,
                 formal_registry=None, notebook_dir=None, transaction_root=None):
        self.root = Path(root); self.papers_dir = Path(papers_dir)
        self._formal_registry = formal_registry
        self._notebook_dir = notebook_dir
        self._transaction_root = transaction_root

    def _resolve_category(self, name: str, *, allow_pending: bool = False) -> Path:
        if name == "_pending":
            if not allow_pending:
                raise ValueError("_pending is not a reliable writer category")
            direct = self.root / name
            if direct.is_dir():
                return direct
            raise FileNotFoundError(f"_pending directory not found: {direct}")
        # Exact directory name match (directory_name == keyword_zh by contract)
        direct = self.root / name
        if direct.is_dir() and (name == "all" or name not in SYSTEM_DIRS):
            return direct
        # Fallback: scan .category.json for category_id lookup
        for path in list_categories(self.root):
            try:
                data = json.loads((path / ".category.json").read_text(encoding="utf-8"))
            except Exception:
                continue
            if name == data.get("category_id"):
                return path
        raise FileNotFoundError(f"catalog category not found: {name}")

    def _pending_papers(self) -> set[str]:
        """Return paper_names currently in _pending."""
        pending_dir = self.root / "_pending"
        if not pending_dir.is_dir():
            return set()
        names: set[str] = set()
        for child in pending_dir.iterdir():
            if child.name == ".category.json":
                continue
            if inspect_paper_link(child) is not None:
                names.add(child.name)
        return names

    def list_papers(self, categories: list[str] | None = None, *, mode: str = "union",
                    allow_pending: bool = False) -> list[dict]:
        if (self.root / ".state" / "DIRTY").exists():
            raise RuntimeError("catalog folder state is DIRTY")
        names = categories or ["all"]
        # Writer safety gate for non-"all" category access
        if names != ["all"]:
            self.assert_writer_safe()
        # Only check pending for non-"all" reads; "all" always works
        pending = self._pending_papers()
        if names != ["all"] and pending and not allow_pending:
            raise RuntimeError(
                f"Catalog classification incomplete: {len(pending)} papers pending. "
                f"Run classification before keyword-based writing, or pass --allow-pending "
                f"to proceed with all/_pending as supplementary scan."
            )
        if pending and allow_pending:
            names = list(dict.fromkeys([*names, "all", "_pending"]))
        sets = []
        rows_by_number: dict[str, dict] = {}
        for name in names:
            if name == "_pending" and not allow_pending:
                continue
            rows = read_category_members(
                self._resolve_category(name, allow_pending=allow_pending),
                papers_dir=self.papers_dir,
            )
            numbers = {row["paper_number"] for row in rows}
            sets.append(numbers)
            rows_by_number.update({
                row["paper_number"]: {
                    **row["catalog"],
                    "formal_directory": str(row["directory"]),
                }
                for row in rows
            })
        selected = (
            set.intersection(*sets) if mode == "intersection" and sets
            else set.union(*sets) if sets
            else set()
        )
        return [rows_by_number[number] for number in sorted(selected)]

    def get(self, identity: str) -> dict | None:
        return next(
            (row for row in self.list_papers(["all"])
             if identity in {row.get("paper_number"), row.get("paper_name")}),
            None,
        )

    def compact_batches(self, categories: list[str] | None = None, *, mode: str = "union",
                        batch_size: int = 15, allow_pending: bool = False):
        if not 10 <= batch_size <= 20:
            raise ValueError("writer Catalog batch_size must be between 10 and 20")
        names = categories or ["all"]
        # Writer safety gate for non-"all" category access
        if names != ["all"]:
            self.assert_writer_safe()
        papers = self.list_papers(categories, mode=mode, allow_pending=allow_pending)
        for offset in range(0, len(papers), batch_size):
            yield papers[offset:offset + batch_size]

    def validate(self) -> list[str]:
        errors: list[str] = []
        if (self.root / ".state" / "DIRTY").exists():
            errors.append("catalog folder state is DIRTY")
        for category in [self.root / "all", *list_categories(self.root)]:
            try:
                read_category_members(category, papers_dir=self.papers_dir)
            except Exception as exc:
                errors.append(str(exc))
        return errors

    def assert_writer_safe(self) -> None:
        """Assert catalog folder state is safe for writer operations.

        Runs ``validate()`` and a doctor diagnostic, then raises RuntimeError
        with a summary if the state is not writer-safe.
        """
        errors = self.validate()
        if self._formal_registry is not None:
            from src.catalog_folders.validation import doctor
            report = doctor(
                root=self.root,
                formal_registry=self._formal_registry,
                notebook_dir=self._notebook_dir,
                transaction_root=self._transaction_root,
            )
            if not report.get("writer_category_safe", False):
                doctor_errors = report.get("errors", [])
                all_errors = list(dict.fromkeys(errors + doctor_errors))
                raise RuntimeError(
                    f"Catalog folder state is not writer-safe. "
                    f"Errors: {len(all_errors)}. "
                    f"First 5: {all_errors[:5]}"
                )
        elif errors:
            raise RuntimeError(
                f"Catalog folder validation errors: {errors}"
            )

    def status(self) -> dict:
        """Return a structured status report for monitoring and gates."""
        errors = self.validate()
        unfinished_transactions: list[str] = []
        folder_integrity_safe = len(errors) == 0 and not (self.root / ".state" / "DIRTY").exists()
        classification_complete = False
        writer_category_safe = False
        if self._formal_registry is not None:
            from src.catalog_folders.validation import doctor
            report = doctor(
                root=self.root,
                formal_registry=self._formal_registry,
                notebook_dir=self._notebook_dir,
                transaction_root=self._transaction_root,
            )
            folder_integrity_safe = report.get("folder_integrity_safe", False)
            classification_complete = report.get("classification_complete", False)
            writer_category_safe = report.get("writer_category_safe", False)
            # Collect unfinished transaction journal paths
            apply_journal_dir = (self._transaction_root or self.root / ".state") / "apply_journal"
            if apply_journal_dir.is_dir():
                for journal_file in apply_journal_dir.rglob("*.json"):
                    try:
                        data = json.loads(journal_file.read_text(encoding="utf-8"))
                        if data.get("state") not in ("committed", "rolled_back"):
                            unfinished_transactions.append(str(journal_file))
                    except Exception:
                        unfinished_transactions.append(str(journal_file))
        return {
            "folder_integrity_safe": folder_integrity_safe,
            "classification_complete": classification_complete,
            "writer_category_safe": writer_category_safe,
            "catalog_errors": errors,
            "unfinished_transactions": unfinished_transactions,
        }


def list_categories(root: Path) -> list[Path]:
    return sorted(
        path for path in Path(root).iterdir()
        if path.is_dir() and path.name not in SYSTEM_DIRS and not path.name.startswith(".")
    )


def read_category_members(category_dir: Path, *, papers_dir: Path) -> list[dict]:
    """Read all paper links in a category directory.

    Each member must be a managed junction/symlink whose target is under
    ``papers_dir`` and contains valid catalog with matching identities.
    Duplicate ``paper_number`` or ``paper_name`` values within the same
    category are rejected.
    """
    papers_root = Path(papers_dir).resolve()
    result: list[dict] = []
    seen_numbers: dict[str, str] = {}  # paper_number → link_name
    seen_names: dict[str, str] = {}    # paper_name → paper_number
    for path in sorted(Path(category_dir).iterdir()):
        if path.name == ".category.json":
            continue
        link = inspect_paper_link(path)
        if link is None:
            raise ValueError(f"unmanaged category member (not a valid link): {path}")
        if path.name != link.target.name:
            raise ValueError(f"category link name mismatch: {path} -> {link.target}")
        if _PAPER_NUMBER_RE.match(path.name):
            raise ValueError(f"old number-named link: {path}")
        if link.target.parent != papers_root:
            raise ValueError(f"category link target escapes papers dir: {path}")
        paper_name = link.target.name
        catalog_path = link.target / f"{paper_name}.catalog.json"
        if not catalog_path.is_file():
            raise ValueError(f"category link target missing catalog: {path}")
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        # Verify paper_name matches
        if catalog.get("paper_name") != paper_name:
            raise ValueError(f"category link identity mismatch: {path}")
        # Verify paper_number validity
        paper_number = str(catalog.get("paper_number") or "")
        if not paper_number:
            raise ValueError(f"category link target missing paper_number in catalog: {path}")
        if not _PAPER_NUMBER_RE.match(paper_number):
            raise ValueError(
                f"category link target has invalid paper_number "
                f"({paper_number!r}) in catalog: {path}"
            )
        if not catalog.get("paper_name"):
            raise ValueError(f"category link target missing paper_name in catalog: {path}")
        # Verify marker matches catalog
        markers = sorted(link.target.glob("*.paper.number"))
        if len(markers) == 1:
            import json as _json
            marker = _json.loads(markers[0].read_text(encoding="utf-8"))
            if marker.get("paper_number") != paper_number:
                raise ValueError(
                    f"marker paper_number ({marker.get('paper_number')}) "
                    f"!= catalog paper_number ({paper_number}): {path}"
                )
        # Detect duplicates
        if paper_number in seen_numbers:
            raise ValueError(
                f"duplicate paper_number {paper_number} in category: "
                f"{seen_numbers[paper_number]} and {path.name}"
            )
        if paper_name in seen_names:
            raise ValueError(
                f"duplicate paper_name {paper_name} in category: "
                f"paper_numbers {seen_names[paper_name]} and {paper_number}"
            )
        seen_numbers[paper_number] = path.name
        seen_names[paper_name] = paper_number
        result.append({
            "paper_number": paper_number,
            "paper_name": paper_name,
            "directory": link.target,
            "catalog": catalog,
        })
    return result


def create_safe_catalog_reader() -> CatalogFolderReader:
    """Factory that constructs a fully-wired CatalogFolderReader for production use.

    Includes ``formal_registry``, ``notebook_dir``, and ``transaction_root``
    so that ``assert_writer_safe()`` and ``status()`` can run the complete
    doctor diagnostic.  All production callers MUST use this factory instead
    of constructing ``CatalogFolderReader`` directly.

    The notebook directory resolves from the active v4 discovery workspace;
    resolution is fail-closed and raises
    :class:`~src.discovery.runtime_context.DiscoveryRuntimeUnavailableError`
    when no active workspace exists.
    """
    from config.settings import (
        CATALOG_FOLDER_ROOT,
        PAPERS_DIR, PAPER_NUMBER_LEDGER_PATH, TRANSACTION_ROOT,
    )
    from src.catalog_folders.formal_registry import FormalPaperRegistry
    from src.discovery.runtime_context import resolve_active_runtime
    from src.library.paper_number_ledger import PaperNumberLedger

    return CatalogFolderReader(
        root=CATALOG_FOLDER_ROOT,
        papers_dir=PAPERS_DIR,
        formal_registry=FormalPaperRegistry(
            papers_dir=PAPERS_DIR,
            ledger=PaperNumberLedger(PAPER_NUMBER_LEDGER_PATH),
        ),
        notebook_dir=resolve_active_runtime().notebook_root,
        transaction_root=TRANSACTION_ROOT,
    )
