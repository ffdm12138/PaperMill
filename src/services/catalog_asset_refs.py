"""Catalog asset-reference canonicalization helpers."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Literal


CatalogStage = Literal["paper_raw", "formalized", "papers"]


def _prefix(*, paper_number: str, paper_id: str | None, stage: CatalogStage) -> str:
    if stage in {"formalized", "papers"}:
        return paper_id or ""
    return paper_id or paper_number


def canonicalize_catalog_asset_refs(
    catalog: dict,
    *,
    folder: Path,
    paper_number: str,
    paper_id: str | None,
    stage: CatalogStage,
) -> dict:
    """Return a catalog copy whose same-folder asset refs match its current stage."""
    out = deepcopy(catalog)
    for legacy_key in ("paper_number", "paper_id", "asset_refs"):
        out.pop(legacy_key, None)
    prefix = _prefix(paper_number=paper_number, paper_id=paper_id, stage=stage)
    if not prefix:
        raise ValueError("catalog asset refs require paper_id for formalized/papers stages")
    locator = out.get("library_locator") if isinstance(out.get("library_locator"), dict) else {}
    asset_refs = locator.get("asset_refs") if isinstance(locator.get("asset_refs"), dict) else {}
    asset_refs.update({
        "markdown": f"{prefix}.md",
        "pdf": f"{prefix}.pdf",
        "metadata": f"{prefix}.metadata.json",
        "catalog": f"{prefix}.catalog.json",
        "asset_manifest": f"{prefix}.asset_manifest.json",
        "images_dir": "images/",
    })
    locator.update({
        "paper_number": paper_number,
        "paper_id": paper_id or "",
        "paper_dir": str(folder.as_posix()),
        "asset_refs": asset_refs,
    })
    out["library_locator"] = locator
    provenance = out.get("provenance") if isinstance(out.get("provenance"), dict) else {}
    old_markdown = str(provenance.get("markdown_path") or "")
    new_markdown = f"{prefix}.md"
    if old_markdown and old_markdown != new_markdown and not provenance.get("original_markdown_path"):
        provenance["original_markdown_path"] = old_markdown
    provenance["markdown_path"] = new_markdown
    out["provenance"] = provenance
    return out


def validate_catalog_asset_refs(
    folder: Path,
    catalog: dict,
    *,
    paper_number: str,
    paper_id: str | None,
    stage: CatalogStage,
) -> list[str]:
    """Validate same-folder catalog refs for the current stage."""
    errors: list[str] = []
    try:
        expected = canonicalize_catalog_asset_refs(
            catalog,
            folder=folder,
            paper_number=paper_number,
            paper_id=paper_id,
            stage=stage,
        )
    except Exception as exc:
        return [str(exc)]
    locator = catalog.get("library_locator") if isinstance(catalog.get("library_locator"), dict) else {}
    refs = locator.get("asset_refs") if isinstance(locator.get("asset_refs"), dict) else {}
    expected_refs = ((expected.get("library_locator") or {}).get("asset_refs") or {})
    for key in ("markdown", "pdf", "metadata", "catalog", "asset_manifest", "images_dir"):
        actual = str(refs.get(key) or "")
        want = str(expected_refs.get(key) or "")
        if actual != want:
            errors.append(f"catalog.library_locator.asset_refs.{key} must be {want}, got {actual or '(empty)'}")
        if want and not (folder / want).exists():
            errors.append(f"catalog.library_locator.asset_refs.{key} does not exist: {want}")
    provenance = catalog.get("provenance") if isinstance(catalog.get("provenance"), dict) else {}
    actual_markdown = str(provenance.get("markdown_path") or "")
    expected_markdown = str((expected.get("provenance") or {}).get("markdown_path") or "")
    if actual_markdown != expected_markdown:
        errors.append(f"catalog.provenance.markdown_path must be {expected_markdown}, got {actual_markdown or '(empty)'}")
    elif expected_markdown and not (folder / expected_markdown).exists():
        errors.append(f"catalog.provenance.markdown_path does not exist: {expected_markdown}")
    return errors
