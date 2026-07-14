"""Compatibility entrypoint delegating to the sole active formal validator."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.discovery.models import normalize_doi
from src.library.paper_number_ledger import PaperNumberLedger
from src.library.validation import validate_formal_paper
from src.metadata.schema import metadata_doi


@dataclass(frozen=True)
class FormalWorkspaceValidationResult:
    valid: bool
    paper_number: str | None
    normalized_doi: str | None
    errors: tuple[str, ...]
    unverifiable: bool = False


def validate_formal_paper_workspace(
    workspace: Path, *, ledger: PaperNumberLedger,
    mode: Literal["strict", "workspace_only"] = "strict",
) -> FormalWorkspaceValidationResult:
    folder = Path(workspace)
    errors: list[str] = []
    paper_number = ""
    doi = ""
    unverifiable = False
    try:
        info = validate_formal_paper(folder)
        paper_number = info["paper_number"]
        doi = normalize_doi(metadata_doi(info["metadata"]))
    except Exception as exc:
        errors.append(str(exc))
    if mode == "strict" and not errors:
        item = ((ledger.load().get("items") or {}).get(paper_number) or {})
        if item.get("state") != "active" or item.get("paper_name") != folder.name or item.get("folder_name") != folder.name:
            errors.append("active ledger entry missing formal identity")
    return FormalWorkspaceValidationResult(
        valid=not errors and not unverifiable, paper_number=paper_number or None,
        normalized_doi=doi or None, errors=tuple(errors), unverifiable=unverifiable,
    )
