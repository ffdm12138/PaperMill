"""Canonical accessor for an active numeric paper_raw workspace."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

PAPER_NUMBER_RE=re.compile(r"^[0-9]{16}$")

@dataclass(frozen=True)
class PaperRawWorkspace:
    root: Path
    paper_number: str

    @classmethod
    def open(cls, root: str|Path, paper_number: str, *, require_exists: bool=True, require_marker: bool=True) -> "PaperRawWorkspace":
        root=Path(root); number=str(paper_number)
        if not PAPER_NUMBER_RE.fullmatch(number): raise ValueError(f"paper_number must be 16 digits: {number!r}")
        folder=root/number
        if require_exists and not folder.is_dir(): raise FileNotFoundError(folder)
        workspace=cls(folder,number)
        if require_marker and folder.exists(): workspace.validate_marker()
        return workspace

    @classmethod
    def from_path(cls, folder: str|Path, *, require_marker: bool=True) -> "PaperRawWorkspace":
        folder=Path(folder)
        if not PAPER_NUMBER_RE.fullmatch(folder.name): raise ValueError("active paper_raw workspace must use a 16-digit directory")
        workspace=cls(folder,folder.name)
        if require_marker: workspace.validate_marker()
        return workspace

    @property
    def marker(self): return self.root/f"{self.paper_number}.paper.number"
    @property
    def metadata(self): return self.root/f"{self.paper_number}.metadata.json"
    @property
    def metadata_match(self): return self.root/f"{self.paper_number}.metadata_match.json"
    @property
    def metadata_freeze(self): return self.root/f"{self.paper_number}.metadata_freeze.json"
    @property
    def pdf(self): return self.root/f"{self.paper_number}.pdf"
    @property
    def markdown(self): return self.root/f"{self.paper_number}.md"
    @property
    def conversion(self): return self.root/f"{self.paper_number}.conversion.json"
    @property
    def catalog_task(self): return self.root/f"{self.paper_number}.catalog_task.json"
    @property
    def catalog(self): return self.root/f"{self.paper_number}.catalog.json"
    @property
    def catalog_freeze(self): return self.root/f"{self.paper_number}.catalog_freeze.json"
    @property
    def formalization(self): return self.root/f"{self.paper_number}.formalization.json"
    @property
    def status(self): return self.root/".import_status.json"
    @property
    def images(self): return self.root/"images"
    @property
    def discovery_receipt(self): return self.root/f"{self.paper_number}.discovery_receipt.json"
    @property
    def source_records(self): return self.root/"source_records"
    @property
    def lock(self): return self.root/".workspace.lock"

    def validate_marker(self) -> dict:
        try: marker=json.loads(self.marker.read_text(encoding="utf-8"))
        except (OSError,json.JSONDecodeError) as exc: raise ValueError(f"invalid paper-number marker: {exc}") from exc
        if marker.get("paper_number")!=self.paper_number: raise ValueError("marker.paper_number does not match workspace")
        if marker.get("folder_name")!=self.root.name: raise ValueError("marker.folder_name does not match workspace")
        if marker.get("state") not in {"allocating","reserved","active","abandoned"}: raise ValueError("marker.state is not a ledger lifecycle state")
        return marker


def validate_workspace_contents(
    path: str | Path,
    expected_paper_number: str,
    *,
    layout: str = "raw",
    require_canonical_directory_name: bool = True,
    papers_dir: Path | None = None,
    paper_raw_root: Path | None = None,
) -> dict:
    """Validate raw contents independently of a hidden staging directory name."""
    if layout != "raw":
        raise ValueError(f"unsupported workspace layout: {layout}")
    number = str(expected_paper_number)
    if not PAPER_NUMBER_RE.fullmatch(number):
        raise ValueError("expected_paper_number must be 16 digits")
    folder = Path(path)
    if require_canonical_directory_name and folder.name != number:
        raise ValueError("raw workspace directory name must equal paper_number")
    marker_path = folder / f"{number}.paper.number"
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid raw marker: {exc}") from exc
    if marker.get("paper_number") != number or marker.get("folder_name") != number:
        raise ValueError("raw marker identity mismatch")
    from src.metadata.freeze import assert_metadata_frozen
    from src.catalog.freeze import assert_catalog_frozen

    metadata_freeze = assert_metadata_frozen(
        folder, number, asset_prefix=None if require_canonical_directory_name else number
    )
    catalog_freeze = assert_catalog_frozen(
        folder, number, papers_dir=papers_dir, paper_raw_root=paper_raw_root,
        require_canonical_directory_name=require_canonical_directory_name,
    )
    return {"paper_number": number, "marker": marker, "metadata_freeze": metadata_freeze, "catalog_freeze": catalog_freeze}
