"""DEPRECATED — 已废弃，不要用于真实迁移。

此脚本的 catalog 部分（``_migrate_catalog``）仍生成 catalog v3.0 结构，
会对 v3.1 ``empty_catalog()`` 访问不存在的顶层 ``asset_refs`` / ``naming`` /
``content_notes`` 字段，导致 ``KeyError``。它已被
``scripts/one_shot_migrations/migrate_catalog_v3_0_to_v3_1.py``（catalog 部分）
取代；metadata v1.1→v2.0 迁移请用 ``src/services/metadata_resolver.py`` 等运行时代码。

仅保留作历史参考，正常 docs/SOP 不得引用，不得在真实 ``data/paper_raw`` /
``data/papers`` 上运行。如需 catalog 迁移，使用
``migrate_catalog_v3_0_to_v3_1.py``。

原描述（已过时，仅存档）：
- metadata v2.0: citation-only / script-only
- catalog v3.0: LLM content index  # 注意：v3.0 已被 v3.1 取代

此脚本不再位于 scripts/one_shot_migrations，已移至 scripts/legacy/。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config.settings import ALL_CATALOG_PATH, PAPER_RAW_DIR, PAPERS_DIR, PAPER_NUMBER_LEDGER_PATH
from src.services.asset_manifest import write_asset_manifest
from src.services.v2_library import (
    AllCatalogBuilder,
    PaperNumberLedger,
    empty_catalog,
    empty_metadata,
    paper_id_from_metadata_catalog,
    validate_catalog_schema,
    validate_metadata_schema,
)
from src.utils.atomic_io import atomic_write_json


OLD_TITLE_KEYS = ("short_zh", "translated_zh")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _paper_number(folder: Path, old_metadata: dict[str, Any]) -> str:
    for key in ("paper_number", "paper_raw_id"):
        value = str(old_metadata.get(key) or "")
        if re.fullmatch(r"\d{16}", value):
            return value
    for marker in sorted(folder.glob("*.paper.number")):
        if re.fullmatch(r"\d{16}", marker.stem):
            return marker.stem
    return folder.name if re.fullmatch(r"\d{16}", folder.name) else ""


def _copy_nested(dst: dict[str, Any], src: dict[str, Any], key: str) -> None:
    if isinstance(src.get(key), dict) and isinstance(dst.get(key), dict):
        for child_key, value in src[key].items():
            if child_key in dst[key]:
                dst[key][child_key] = value


def _migrate_metadata(old: dict[str, Any], *, paper_number: str, folder: Path, prefix: str, apply: bool) -> tuple[dict[str, Any], list[str]]:
    source_type = str(old.get("source_type") or (old.get("source") or {}).get("kind") or "manual_pdf")
    new = empty_metadata(paper_number, source_type=source_type)
    warnings: list[str] = []
    for key in ("paper_id", "entry_type", "year", "language"):
        if key in old:
            new[key] = old[key]
    for key in ("title", "first_author", "date", "container", "publication", "identifiers", "links", "metadata_match"):
        _copy_nested(new, old, key)
    if isinstance(old.get("authors"), list):
        new["authors"] = deepcopy(old["authors"])
    old_source = old.get("source") if isinstance(old.get("source"), dict) else {}
    new["source"].update({
        "kind": old_source.get("kind") or source_type,
        "provider": old_source.get("provider") or "",
        "query": old_source.get("query") or "",
        "retrieved_at": old_source.get("retrieved_at") or "",
        "raw_record_path": old_source.get("raw_record_path") or "",
    })
    if old_source.get("raw_record"):
        provider = str(new["source"].get("provider") or "metadata").lower() or "metadata"
        rel = f"source_records/{provider}.json"
        new["source"]["raw_record_path"] = rel
        warnings.append("moved source.raw_record to sidecar")
        if apply:
            atomic_write_json(folder / rel, old_source["raw_record"], indent=2)
    for forbidden in ("abstract", "keywords", "pdf", "content", "notes", "bibtex"):
        if forbidden in old:
            warnings.append(f"removed metadata.{forbidden}")
    if apply:
        write_asset_manifest(folder, prefix=prefix, paper_number=paper_number, paper_id=new.get("paper_id") or "", stage="papers" if folder.parent.name == "papers" else "paper_raw")
    return new, warnings


def _title_candidate(old_metadata: dict[str, Any], old_catalog: dict[str, Any], folder: Path) -> tuple[str, str]:
    title_obj = old_metadata.get("title") if isinstance(old_metadata.get("title"), dict) else {}
    for key in OLD_TITLE_KEYS:
        value = str(title_obj.get(key) or "").strip()
        if value:
            return value, f"migrated_from_metadata_{key}"
    ci = old_catalog.get("content_identity") if isinstance(old_catalog.get("content_identity"), dict) else {}
    value = str(ci.get("content_title_zh") or ci.get("content_title") or "").strip()
    if value:
        return value, "migrated_from_content_title"
    parts = folder.name.split("_", 2)
    if len(parts) == 3 and re.search(r"[\u4e00-\u9fff]", parts[2]):
        return parts[2], "migrated_from_existing_paper_id"
    return "", "needs_manual_naming_title"


def _migrate_catalog(old: dict[str, Any], old_metadata: dict[str, Any], *, folder: Path, prefix: str, paper_number: str) -> tuple[dict[str, Any], list[str]]:
    new = empty_catalog()
    warnings: list[str] = []
    for key in ("paper_id",):
        if old.get(key):
            new[key] = old[key]
    new["paper_number"] = paper_number
    old_refs = old.get("asset_refs") if isinstance(old.get("asset_refs"), dict) else {}
    new["asset_refs"].update({k: v for k, v in old_refs.items() if k in new["asset_refs"]})
    ci = old.get("content_identity") if isinstance(old.get("content_identity"), dict) else {}
    new["content_identity"]["content_title_zh"] = str(ci.get("content_title_zh") or ci.get("content_title") or "")
    candidates = ci.get("content_title_original_candidates")
    if not isinstance(candidates, list):
        candidates = ci.get("md_title_candidates") if isinstance(ci.get("md_title_candidates"), list) else []
    new["content_identity"]["content_title_original_candidates"] = candidates
    new["content_identity"]["content_language"] = str(ci.get("content_language") or "")
    new["content_identity"]["document_type"] = str(ci.get("document_type") or "")
    naming_title, source = _title_candidate(old_metadata, old, folder)
    new["naming"] = {
        "paper_id_title_zh": naming_title,
        "paper_id_title_source": source,
        "paper_id_title_confidence": 0.8 if naming_title else 0.0,
        "paper_id_title_warnings": [] if naming_title else ["needs_manual_naming_title"],
    }
    for key in ("classification", "screening", "research_card", "evidence_profile", "content_notes", "provenance"):
        if isinstance(old.get(key), dict):
            new[key].update(old[key])
    if new.get("screening", {}).get("read_decision") != "pending":
        new["screening"]["read_decision"] = "pending"
        warnings.append("screening.read_decision reset to pending")
    if not isinstance(old.get("terminology"), dict):
        warnings.append("terminology initialized as empty object; needs LLM enrichment")
    else:
        new["terminology"] = old["terminology"]
    if "content_title" in ci:
        warnings.append("migrated content_identity.content_title to content_title_zh")
    if "md_title_candidates" in ci:
        warnings.append("migrated content_identity.md_title_candidates to content_title_original_candidates")
    if folder.name != prefix and not re.fullmatch(r"\d{16}", folder.name):
        new["asset_refs"].update({
            "markdown": f"{prefix}.md",
            "pdf": f"{prefix}.pdf",
            "metadata": f"{prefix}.metadata.json",
            "catalog": f"{prefix}.catalog.json",
            "images_dir": "images/",
        })
        new["provenance"]["markdown_path"] = f"{prefix}.md"
    return new, warnings


def _workspace_prefix(folder: Path) -> str | None:
    metadata_files = sorted(folder.glob("*.metadata.json"))
    if metadata_files:
        return metadata_files[0].name.removesuffix(".metadata.json")
    return None


def _process_folder(folder: Path, *, apply: bool) -> dict[str, Any] | None:
    prefix = _workspace_prefix(folder)
    if not prefix:
        return None
    metadata_path = folder / f"{prefix}.metadata.json"
    catalog_path = folder / f"{prefix}.catalog.json"
    old_metadata = _read_json(metadata_path)
    old_catalog = _read_json(catalog_path)
    number = _paper_number(folder, old_metadata)
    if not number:
        return {"folder": str(folder), "status": "needs_manual_paper_number"}
    new_metadata, metadata_warnings = _migrate_metadata(old_metadata, paper_number=number, folder=folder, prefix=prefix, apply=apply)
    new_catalog, catalog_warnings = _migrate_catalog(old_catalog, old_metadata, folder=folder, prefix=prefix, paper_number=number)
    if not new_catalog.get("paper_id") and old_metadata.get("paper_id"):
        new_catalog["paper_id"] = old_metadata["paper_id"]
    item = {
        "folder": str(folder),
        "prefix": prefix,
        "paper_number": number,
        "status": "migrated" if apply else "planned",
        "warnings": metadata_warnings + catalog_warnings,
        "metadata_errors": validate_metadata_schema(new_metadata),
        "catalog_errors": validate_catalog_schema(new_catalog),
    }
    try:
        expected = paper_id_from_metadata_catalog(new_metadata, new_catalog)
        if folder.parent.name == "papers" and folder.name != expected:
            item["paper_id_mismatch"] = {"folder": folder.name, "expected": expected}
    except Exception as exc:
        item["paper_id_error"] = str(exc)
    if apply:
        atomic_write_json(metadata_path, new_metadata, indent=2)
        atomic_write_json(catalog_path, new_catalog, indent=2)
    return item


def _iter_workspaces(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [p for p in sorted(root.iterdir()) if p.is_dir() and not p.name.startswith(".") and p.name != "quarantine"]


def main() -> int:
    # DEPRECATED — 此脚本的 catalog 部分对 v3.1 会 KeyError，已被
    # migrate_catalog_v3_0_to_v3_1.py 取代。CLI 入口直接拒绝运行，
    # 内部函数保留作历史参考（测试可 import 并验证已知损坏状态）。
    raise SystemExit(
        "DEPRECATED unsafe migration; "
        "use scripts/one_shot_migrations/migrate_catalog_v3_0_to_v3_1.py"
    )


if __name__ == "__main__":
    raise SystemExit(main())
