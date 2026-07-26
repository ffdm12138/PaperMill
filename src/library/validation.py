"""Authoritative validation for one installed Catalog v3.2 formal paper."""
from __future__ import annotations

import json
from pathlib import Path

from src.catalog.freeze import assert_formal_catalog_frozen
from src.catalog.schema import validate_catalog_v32
from src.utils.file_fingerprint import compute_sha256
from src.metadata.freeze import assert_metadata_frozen
from src.metadata.schema import validate_metadata_schema


MANIFEST_SCHEMA_VERSION = "2.0"
FORMAL_FILE_KEYS = {
    "pdf": "{paper_name}.pdf",
    "markdown": "{paper_name}.md",
    "metadata": "{paper_name}.metadata.json",
    "catalog": "{paper_name}.catalog.json",
    "metadata_match": "{paper_name}.metadata_match.json",
    "metadata_freeze": "{paper_name}.metadata_freeze.json",
    "catalog_task": "{paper_name}.catalog_task.json",
    "catalog_freeze": "{paper_name}.catalog_freeze.json",
    "conversion_manifest": "{paper_name}.conversion.json",
    "paper_number_marker": "{paper_number}.paper.number",
    "images_dir": "images/",
    "source_records_dir": "source_records/",
}


def _read_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _tree_hashes(folder: Path, relative_dir: str) -> dict[str, str]:
    root = folder / relative_dir
    if not root.is_dir():
        return {}
    return {
        path.relative_to(folder).as_posix(): compute_sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def validate_formal_paper(folder: Path, *, expected_paper_name: str | None = None) -> dict:
    paper_name = expected_paper_name or folder.name
    errors: list[str] = []
    markers = sorted(folder.glob("*.paper.number"))
    manifests = sorted(folder.glob("*.asset_manifest.json"))
    if len(markers) != 1:
        errors.append("formal paper requires exactly one paper-number marker")
    if len(manifests) != 1 or (manifests and manifests[0].name != f"{paper_name}.asset_manifest.json"):
        errors.append("formal paper requires exactly one canonical asset manifest")

    marker: dict = {}
    manifest: dict = {}
    metadata: dict = {}
    catalog: dict = {}
    try:
        if markers:
            marker = _read_json(markers[0], "paper-number marker")
        if manifests:
            manifest = _read_json(manifests[0], "asset manifest")
        metadata = _read_json(folder / f"{paper_name}.metadata.json", "metadata")
        catalog = _read_json(folder / f"{paper_name}.catalog.json", "catalog")
    except ValueError as exc:
        errors.append(str(exc))

    paper_number = str(marker.get("paper_number") or "")
    if not (len(paper_number) == 16 and paper_number.isdigit()):
        errors.append("formal marker paper_number invalid")
    if marker.get("state") != "active":
        errors.append("formal marker state must be active")
    identities = (
        marker.get("folder_name"), marker.get("planned_paper_name"),
        catalog.get("paper_name"), manifest.get("paper_name"),
    )
    if any(value != paper_name for value in identities):
        errors.append("formal paper_name/marker/catalog/manifest mismatch")
    if expected_paper_name is None and folder.name != paper_name:
        errors.append("formal directory name mismatch")
    if metadata.get("paper_number") != paper_number or catalog.get("paper_number") != paper_number:
        errors.append("formal metadata/catalog paper_number mismatch")
    if manifest.get("paper_number") != paper_number:
        errors.append("formal manifest paper_number mismatch")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        errors.append(f"asset manifest schema_version must be {MANIFEST_SCHEMA_VERSION}")
    if manifest.get("stage") != "papers":
        errors.append("asset manifest stage must be papers")

    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    expected_files = {
        key: template.format(paper_name=paper_name, paper_number=paper_number)
        for key, template in FORMAL_FILE_KEYS.items()
    }
    for key, relative in expected_files.items():
        if files.get(key) != relative:
            errors.append(f"asset manifest files.{key} must be {relative}")
        path = folder / relative.rstrip("/")
        if key.endswith("_dir"):
            if not path.is_dir():
                errors.append(f"formal directory asset missing: {relative}")
        elif not path.is_file():
            errors.append(f"formal file asset missing: {relative}")

    asset_hashes = manifest.get("asset_hashes") if isinstance(manifest.get("asset_hashes"), dict) else {}
    for key, relative in expected_files.items():
        if key.endswith("_dir"):
            continue
        path = folder / relative
        if path.is_file() and asset_hashes.get(key) != compute_sha256(path):
            errors.append(f"formal manifest hash mismatch: {key}")
    image_hashes = _tree_hashes(folder, "images")
    source_hashes = _tree_hashes(folder, "source_records")
    if manifest.get("image_hashes") != image_hashes:
        errors.append("formal manifest image hashes mismatch")
    if manifest.get("source_record_hashes") != source_hashes:
        errors.append("formal manifest source-record hashes mismatch")

    errors.extend(validate_metadata_schema(metadata))
    if paper_number:
        errors.extend(validate_catalog_v32(catalog, folder, paper_number, asset_prefix=paper_name))
        try:
            assert_metadata_frozen(folder, paper_number, asset_prefix=paper_name)
        except Exception as exc:
            errors.append(f"formal metadata freeze closure invalid: {exc}")
        try:
            assert_formal_catalog_frozen(folder, paper_number, paper_name)
        except Exception as exc:
            errors.append(f"formal catalog freeze closure invalid: {exc}")
    if errors:
        raise ValueError("; ".join(dict.fromkeys(errors)))
    return {
        "paper_number": paper_number,
        "paper_name": paper_name,
        "marker": marker,
        "manifest": manifest,
        "metadata": metadata,
        "catalog": catalog,
    }
