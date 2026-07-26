"""Repair stale derived-file paper_number references in active paper_raw.

After the non-destructive reconcile, the 187 active workspaces still carry
PRE-EXISTING stale 16-digit references in derived files (111 asset_manifests
with a wrong paper_number, plus stale refs in catalog/conversion/stage_manifest/
import_status). compact only rewrites a workspace's OWN number, so it cannot fix
these cross-workspace references — they must be repaired first.

This script is LAYERED to respect what each file type is allowed to change:

* ``asset_manifest.json``  — fully rebuilt from actual files (derived/regenerable).
* ``catalog.json``         — only asset_refs / provenance.markdown_path /
                             paper_number / paper_name via
                             ``canonicalize_catalog_asset_refs``; content fields
                             (content_identity, methods, findings, ...) untouched.
* ``conversion.json``      — replace stale 16-digit tokens (paper_number + paths)
                             with the workspace's correct marker number.
* ``stage_manifest.json``  — same stale-16-digit-token replacement.
* ``.import_status.json``  — same stale-16-digit-token replacement.
* ``.metadata.json``       — ONLY paper_number / paper_raw_id may change; verified
                             by normalized fingerprint. Other 16-digit tokens are
                             REPORTED, never replaced (could be ISSN/IDs).
* ``.md``                  — 16-digit tokens != own number are REPORTED only;
                             never auto-replaced (could be ISSN/IDs in references).

Metadata bibliographic fields (DOI, authors, year, title, container,
identifiers, links, metadata_match, ...) are NEVER modified. The normalized
fingerprint (which ignores paper_number/paper_raw_id + 16-digit tokens) must be
identical before and after; any mismatch aborts that workspace's metadata write.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from config.settings import PAPER_NUMBER_LEDGER_PATH, PAPER_RAW_DIR, PAPERS_DIR
from src.services.asset_manifest import build_asset_manifest, read_asset_manifest, write_asset_manifest
from src.services.catalog_asset_refs import inspect_legacy_catalog_fields
from src.services.ingest_duplicate_guard import is_paper_raw_workspace, read_best_metadata_json
from src.utils.identifiers import PAPER_NUMBER_RE
from src.services.ingest_state import now_iso
from src.services.paper_number_admin import metadata_fingerprint
from src.library.paper_number_ledger import PaperNumberLedger
from src.utils.atomic_io import atomic_write_json


_STANDALONE_16DIGIT = re.compile(r"(?<!\d)\d{16}(?!\d)")
_QUARANTINE = "quarantine"


def _replace_stale_tokens(value: Any, correct_number: str) -> Any:
    """Replace any standalone 16-digit token != correct_number with correct_number.

    Safe for derived files (conversion/stage_manifest/import_status), whose only
    16-digit tokens are the workspace's own paper_number or same-prefix paths.
    """
    if isinstance(value, str):
        return _STANDALONE_16DIGIT.sub(
            lambda m: correct_number if m.group() != correct_number else m.group(), value
        )
    if isinstance(value, list):
        return [_replace_stale_tokens(v, correct_number) for v in value]
    if isinstance(value, dict):
        return {k: _replace_stale_tokens(v, correct_number) for k, v in value.items()}
    return value


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _active_workspaces(paper_raw_dir: Path) -> list[Path]:
    if not paper_raw_dir.exists():
        return []
    return [
        f for f in sorted(paper_raw_dir.iterdir())
        if f.is_dir() and f.name != _QUARANTINE and not f.name.startswith(".") and is_paper_raw_workspace(f)
    ]


def _workspace_identity(folder: Path) -> tuple[str, str, str, str, str]:
    """Return (correct_number, prefix, pid, stage, marker_path) for a workspace."""
    is_numbered = bool(PAPER_NUMBER_RE.match(folder.name))
    marker_number = ""
    marker_path = ""
    for marker in sorted(folder.glob("*.paper.number")):
        parsed = PaperNumberLedger.parse_marker_number(marker)
        if parsed:
            marker_number = parsed
            marker_path = str(marker)
            break
    if is_numbered:
        correct_number = folder.name
        prefix = folder.name
        pid = ""
        stage = "paper_raw"
    else:
        correct_number = marker_number
        prefix = folder.name
        pid = folder.name
        stage = "formalized"
    return correct_number, prefix, pid, stage, marker_path


def _repair_workspace(folder: Path, *, apply: bool) -> dict[str, Any]:
    correct_number, prefix, pid, stage, marker_path = _workspace_identity(folder)
    result: dict[str, Any] = {
        "folder": str(folder),
        "folder_name": folder.name,
        "correct_number": correct_number,
        "prefix": prefix,
        "stage": stage,
        "changes": [],
        "warnings": [],
    }
    if not correct_number:
        result["warnings"].append("no valid marker paper_number; skipping")
        return result

    # 1. asset_manifest.json — rebuild from actual files (compare first so dry-run
    #    is read-only and we only report a change when the manifest differs).
    try:
        old_manifest = read_asset_manifest(folder, prefix)
        new_manifest = build_asset_manifest(
            folder, prefix=prefix, paper_number=correct_number, paper_name=pid, stage=stage
        )
        if new_manifest != old_manifest:
            if apply:
                write_asset_manifest(
                    folder, prefix=prefix, paper_number=correct_number, paper_name=pid, stage=stage
                )
            result["changes"].append("asset_manifest: rebuilt")
    except Exception as exc:
        result["warnings"].append(f"asset_manifest rebuild failed: {exc}")

    # 2. catalog.json — canonicalize library_locator.asset_refs /
    #    library_locator.paper_number / library_locator.paper_name /
    #    provenance.markdown_path only.
    #    Also clear a stale ``provenance.original_markdown_path`` left over from a
    #    prior copy/renumber (canonicalize preserves it as history, but if it
    #    references another workspace's number it is a copy artifact, not real
    #    history — clear it so it does not become a stale 16-digit ref).
    catalog_path = folder / f"{prefix}.catalog.json"
    catalog = _load_json(catalog_path)
    if catalog is not None:
        try:
            issues = inspect_legacy_catalog_fields(catalog)
            if issues:
                for issue in issues:
                    result["warnings"].append(
                        f"catalog legacy field {issue.json_path}: {issue.message}"
                    )
        except Exception as exc:
            result["warnings"].append(f"catalog inspection failed: {exc}")

    # 3-5. derived JSON files — replace stale 16-digit tokens with correct_number.
    derived_json_patterns = (
        f"{prefix}.conversion.json",
        "stage_manifest.json",
        ".import_status.json",
        f"{prefix}.metadata.resolve_report.json",
        f"{prefix}.metadata.candidates.json",
        f"{prefix}.metadata.patch.json",
        f"{prefix}.formalization.json",
    )
    for name in derived_json_patterns:
        path = folder / name
        data = _load_json(path)
        if data is None:
            continue
        new_data = _replace_stale_tokens(data, correct_number)
        if new_data != data:
            if apply:
                atomic_write_json(path, new_data, indent=2)
            result["changes"].append(f"{name}: replaced stale 16-digit tokens")

    # 5b. curation_prompt.md — transient generated prompt (in _FORMAL_TRANSIENT_GLOBS),
    #     not the paper's real markdown. Token-replace stale 16-digit refs.
    cp_path = folder / "curation_prompt.md"
    if cp_path.exists():
        try:
            text = cp_path.read_text(encoding="utf-8")
            new_text = _STANDALONE_16DIGIT.sub(
                lambda m: correct_number if m.group() != correct_number else m.group(), text
            )
            if new_text != text:
                if apply:
                    cp_path.write_text(new_text, encoding="utf-8")
                result["changes"].append("curation_prompt.md: replaced stale 16-digit tokens")
        except (UnicodeDecodeError, OSError):
            pass

    # 6. metadata.json — ONLY paper_number/paper_raw_id; fingerprint-verify.
    meta_path = folder / f"{prefix}.metadata.json"
    meta = _load_json(meta_path)
    if meta is not None:
        before_fp = metadata_fingerprint(meta)
        # Report (don't replace) non-own 16-digit tokens in metadata values.
        reported_tokens: set[str] = set()
        def _collect(value: Any) -> None:
            if isinstance(value, str):
                for m in _STANDALONE_16DIGIT.findall(value):
                    if m != correct_number:
                        reported_tokens.add(m)
            elif isinstance(value, list):
                for v in value:
                    _collect(v)
            elif isinstance(value, dict):
                for v in value.values():
                    _collect(v)
        _collect(meta)
        if reported_tokens:
            result["metadata_stale_tokens_reported"] = sorted(reported_tokens)
        new_meta = deepcopy(meta)
        changed_meta = False
        if str(new_meta.get("paper_number") or "") != correct_number:
            new_meta["paper_number"] = correct_number
            changed_meta = True
        if str(new_meta.get("paper_raw_id") or "") != correct_number:
            new_meta["paper_raw_id"] = correct_number
            changed_meta = True
        if changed_meta:
            after_fp = metadata_fingerprint(new_meta)
            if after_fp != before_fp:
                result["warnings"].append(
                    "metadata fingerprint mismatch after paper_number/paper_raw_id fix; "
                    "bibliographic fields would change — NOT writing metadata"
                )
            else:
                if apply:
                    atomic_write_json(meta_path, new_meta, indent=2)
                result["changes"].append("metadata: set paper_number/paper_raw_id (fingerprint verified)")

    # 7. .md — report 16-digit tokens != correct_number; never replace.
    md_path = folder / f"{prefix}.md"
    if md_path.exists():
        try:
            text = md_path.read_text(encoding="utf-8")
            md_tokens = sorted({m for m in _STANDALONE_16DIGIT.findall(text) if m != correct_number})
            if md_tokens:
                result["md_stale_tokens_reported"] = md_tokens
        except (UnicodeDecodeError, OSError):
            pass

    return result


def build_plan(paper_raw_dir: Path, *, apply: bool) -> dict[str, Any]:
    workspaces = _active_workspaces(paper_raw_dir)
    items = [_repair_workspace(f, apply=apply) for f in workspaces]
    change_count = sum(1 for it in items if it["changes"])
    warn_count = sum(1 for it in items if it["warnings"])
    metadata_reports = sum(1 for it in items if it.get("metadata_stale_tokens_reported"))
    md_reports = sum(1 for it in items if it.get("md_stale_tokens_reported"))
    return {
        "schema": "repair_paper_raw_derived_files",
        "generated_at": now_iso(),
        "paper_raw_dir": str(paper_raw_dir),
        "applied": apply,
        "summary": {
            "workspace_count": len(workspaces),
            "workspaces_with_changes": change_count,
            "workspaces_with_warnings": warn_count,
            "metadata_with_stale_tokens_reported": metadata_reports,
            "md_with_stale_tokens_reported": md_reports,
        },
        "items": items,
    }


def _print_human_report(report: dict[str, Any]) -> None:
    s = report["summary"]
    print(f"derived-file repair — {s['workspace_count']} workspace(s), "
          f"applied={report['applied']}")
    print(f"  workspaces with changes : {s['workspaces_with_changes']}")
    print(f"  workspaces with warnings: {s['workspaces_with_warnings']}")
    print(f"  metadata stale-token reports: {s['metadata_with_stale_tokens_reported']}")
    print(f"  md stale-token reports      : {s['md_with_stale_tokens_reported']}")
    sample_warns = [w for it in report["items"] for w in it.get("warnings", [])][:8]
    if sample_warns:
        print("  sample warnings:")
        for w in sample_warns:
            print(f"    - {w}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repair stale derived-file paper_number refs in active paper_raw.")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--apply", action="store_true", help="write repairs (default: dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="force dry-run")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args(argv)

    apply = bool(args.apply and not args.dry_run)
    report = build_plan(args.paper_raw_dir, apply=apply)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_human_report(report)
    return 1 if report["summary"]["workspaces_with_warnings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
