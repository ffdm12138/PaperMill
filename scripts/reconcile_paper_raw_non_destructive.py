"""Non-destructively reconcile ``data/paper_raw`` to the 187 raw PDFs.

This script restores workspaces that were quarantined as "unreferenced" back
into the active ``data/paper_raw/`` root so the active set collapses to exactly
the 187 raw PDFs — WITHOUT re-staging bare PDFs, WITHOUT regenerating metadata,
and WITHOUT overwriting bibliographic fields.

Context: ``data/raw`` holds 187 PDFs (the source of truth). 143 already match
active numbered workspaces; 44 more match workspaces in
``quarantine/unreferenced_workspaces/`` (legacy/untitled folders with real
metadata/conversion/catalog work). 143 + 44 = 187. One active workspace
(``0000000000000142``) is an empty corpse with no PDF — it is archived, not
deleted.

What this script does:
  * Move each unreferenced workspace whose PDF matches a raw PDF back to the
    active ``data/paper_raw/`` root, whole-directory, as-is.
  * Repair ``.paper`` folder-name pollution (e.g. ``0000000000000185.paper``)
    by aligning the folder + file prefixes to the marker number — a pure rename,
    no content change.
  * Archive confirmed empty corpses (no PDF/metadata/md/catalog/images) to
    ``data/transactions/reconcile_paper_raw_<timestamp>/empty_corpses/``.
  * Refuse if any active-only workspace carries real assets (would lose work).

What this script NEVER does:
  * Re-stage bare PDFs, run MinerU, or regenerate metadata/catalog.
  * Modify metadata bibliographic content (DOI/authors/year/title/...).
  * Touch ``quarantine/duplicate_workspaces/``.
  * Move or delete anything in ``data/raw/``.
  * Touch the ledger (compact rebuilds it next).

The subsequent ``reset_paper_number_ledger.py --compact-paper-raw
--protect-metadata`` step renumbers the 187 workspaces to 1..187 and rebuilds
the ledger; this script only restores the set.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import PAPER_NUMBER_LEDGER_PATH, PAPER_RAW_DIR, PAPERS_DIR, RAW_DIR
from src.file_fingerprint import compute_file_hashes
from src.services.ingest_duplicate_guard import (
    find_best_pdf,
    is_paper_raw_workspace,
    read_best_metadata_json,
    resolve_paper_raw_identity,
)
from src.services.ingest_ids import PAPER_NUMBER_RE
from src.services.ingest_state import now_iso
from src.library.paper_number_ledger import PaperNumberLedger
from src.utils.atomic_io import atomic_write_json


_QUARANTINE = "quarantine"
_UNREFERENCED = "unreferenced_workspaces"
_DUPLICATES = "duplicate_workspaces"
_CORPSE_ALLOWED_FILES = {".import_status.json", ".DS_Store", "Thumbs.db"}
_PAPER_POLLUTION_RE = re.compile(r"^(?P<num>\d{16})\.paper$")


def _hash_pdf(path: Path) -> dict[str, Any] | None:
    try:
        h = compute_file_hashes(path)
    except OSError:
        return None
    return {"sha256": str(h["sha256"]).lower(), "md5": str(h["md5"]).lower(),
            "file_size": int(h["file_size"]), "path": str(path)}


def _is_empty_corpse(folder: Path) -> bool:
    for pattern in ("*.pdf", "*.metadata.json", "*.md", "*.catalog.json"):
        if any(folder.glob(pattern)):
            return False
    images = folder / "images"
    if images.exists() and any(p for p in images.rglob("*") if p.is_file()):
        return False
    for child in folder.iterdir():
        if child.is_dir():
            if child.name == "images":
                continue
            if any(p for p in child.rglob("*") if p.is_file()):
                return False
            continue
        if child.name in _CORPSE_ALLOWED_FILES or child.name.endswith(".paper.number"):
            continue
        return False
    return True


def _active_workspace_folders(paper_raw_dir: Path) -> list[Path]:
    if not paper_raw_dir.exists():
        return []
    return [
        f for f in sorted(paper_raw_dir.iterdir())
        if f.is_dir() and f.name != _QUARANTINE and not f.name.startswith(".") and is_paper_raw_workspace(f)
    ]


def _unreferenced_folders(paper_raw_dir: Path) -> list[Path]:
    holder = paper_raw_dir / _QUARANTINE / _UNREFERENCED
    if not holder.exists():
        return []
    return [f for f in sorted(holder.iterdir()) if f.is_dir() and is_paper_raw_workspace(f)]


def _workspace_pdf_sha(folder: Path) -> str | None:
    pdf = find_best_pdf(folder)
    if pdf is None:
        return None
    h = _hash_pdf(pdf)
    return h["sha256"] if h else None


def _content_16digit_tokens(folder: Path, exclude: str) -> set[str]:
    """Return 16-digit tokens found in text/JSON file contents, excluding ``exclude``."""
    tokens: set[str] = set()
    for p in folder.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in {".json", ".md", ".txt", ".number"}:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for m in re.findall(r"(?<!\d)\d{16}(?!\d)", text):
            if m != exclude:
                tokens.add(m)
    return tokens


def _plan_paper_pollution_repair(folder: Path) -> dict[str, Any] | None:
    """If ``folder`` has ``.paper`` name pollution, plan a pure-rename repair.

    Aligns the folder name and file prefixes to the marker number. Returns None
    if no repair needed. Raises ``RuntimeError`` if the repair would require
    content changes (conservative: refuse rather than rewrite content here).
    """
    m = _PAPER_POLLUTION_RE.match(folder.name)
    if not m:
        return None
    folder_number = m.group("num")
    marker_number = ""
    for marker in folder.glob("*.paper.number"):
        marker_number = PaperNumberLedger.parse_marker_number(marker) or ""
        if marker_number:
            break
    if not marker_number:
        raise RuntimeError(f"cannot repair .paper pollution: no valid marker in {folder}")
    # If content references the folder_number (not the marker number), a pure
    # rename would leave stale refs — refuse so the operator can investigate.
    stale = _content_16digit_tokens(folder, marker_number)
    if stale:
        raise RuntimeError(
            f"cannot repair .paper pollution in {folder}: content references "
            f"non-marker 16-digit tokens {sorted(stale)}; manual repair required"
        )
    target_folder_name = marker_number
    file_renames: list[dict[str, str]] = []
    if folder_number != marker_number:
        for p in sorted(folder.iterdir()):
            if p.is_file() and p.name.startswith(folder_number):
                new_name = marker_number + p.name[len(folder_number):]
                file_renames.append({"from": p.name, "to": new_name})
    return {
        "folder_before": folder.name,
        "folder_after": target_folder_name,
        "file_renames": file_renames,
        "marker_number": marker_number,
        "folder_number": folder_number,
    }


def build_plan(
    *,
    raw_dir: Path,
    paper_raw_dir: Path,
    papers_dir: Path,
    expect_count: int,
) -> dict[str, Any]:
    # Preflight
    errors: list[str] = []
    formal = [p for p in papers_dir.iterdir() if p.is_dir() and not p.name.startswith(".")] if papers_dir.exists() else []
    if formal:
        errors.append(f"data/papers is not empty: {len(formal)} dir(s)")
    raw_pdfs = sorted(p for p in raw_dir.glob("*.pdf") if p.is_file())
    if len(raw_pdfs) != expect_count:
        errors.append(f"raw_pdf_count={len(raw_pdfs)} != expect_count={expect_count}")
    raw_hashes: dict[str, Path] = {}
    raw_dup_check: dict[str, list[str]] = {}
    for pdf in raw_pdfs:
        h = _hash_pdf(pdf)
        if h is None:
            continue
        raw_dup_check.setdefault(h["sha256"], []).append(pdf.name)
        raw_hashes[h["sha256"]] = pdf
    raw_internal_dups = {sha: names for sha, names in raw_dup_check.items() if len(names) > 1}
    if raw_internal_dups:
        errors.append(f"data/raw has internal sha256 duplicates: {raw_internal_dups}")

    active_folders = _active_workspace_folders(paper_raw_dir)
    unreferenced_folders = _unreferenced_folders(paper_raw_dir)

    active_sha: dict[str, Path] = {}
    for f in active_folders:
        sha = _workspace_pdf_sha(f)
        if sha:
            active_sha[sha] = f

    # Match raw to active, then unreferenced.
    raw_shas = set(raw_hashes)
    active_matched: list[Path] = [f for sha, f in active_sha.items() if sha in raw_shas]
    active_matched_set = set(active_matched)
    active_only_folders = [f for f in active_folders if f not in active_matched_set]

    unreferenced_matched: list[Path] = []
    unreferenced_only: list[Path] = []
    for f in unreferenced_folders:
        sha = _workspace_pdf_sha(f)
        if sha and sha in raw_shas:
            unreferenced_matched.append(f)
        else:
            unreferenced_only.append(f)

    matched_shas = set(active_sha) | {_workspace_pdf_sha(f) for f in unreferenced_matched if _workspace_pdf_sha(f)}
    raw_only_shas = raw_shas - matched_shas

    # Classify active_only into corpses vs with-assets
    corpses: list[dict[str, Any]] = []
    blocking_with_assets: list[dict[str, Any]] = []
    for f in active_only_folders:
        entry = {"folder": str(f), "folder_name": f.name}
        if _is_empty_corpse(f):
            corpses.append(entry)
        else:
            blocking_with_assets.append(entry)

    # Plan .paper pollution repairs for matched unreferenced workspaces
    repairs: list[dict[str, Any]] = []
    repair_errors: list[str] = []
    for f in unreferenced_matched:
        try:
            plan = _plan_paper_pollution_repair(f)
        except RuntimeError as exc:
            repair_errors.append(str(exc))
            continue
        if plan:
            plan["source_folder"] = str(f)
            repairs.append(plan)

    if repair_errors:
        errors.extend(repair_errors)

    target_reconstructable = (
        not errors
        and len(raw_only_shas) == 0
        and len(blocking_with_assets) == 0
        and (len(active_matched) + len(unreferenced_matched)) == len(raw_pdfs)
    )
    expected_final_active = len(active_matched) + len(unreferenced_matched)

    return {
        "schema": "reconcile_paper_raw_non_destructive_plan",
        "generated_at": now_iso(),
        "raw_dir": str(raw_dir),
        "paper_raw_dir": str(paper_raw_dir),
        "papers_dir": str(papers_dir),
        "expect_count": expect_count,
        "errors": errors,
        "summary": {
            "raw_pdf_count": len(raw_pdfs),
            "active_workspace_count": len(active_folders),
            "active_matched_raw_count": len(active_matched),
            "unreferenced_matched_raw_count": len(unreferenced_matched),
            "unreferenced_only_count": len(unreferenced_only),
            "raw_only_count": len(raw_only_shas),
            "active_only_count": len(active_only_folders),
            "active_only_corpse_count": len(corpses),
            "active_only_with_assets_count": len(blocking_with_assets),
            "expected_final_active_count": expected_final_active,
            "target_reconstructable_without_metadata_loss": target_reconstructable,
        },
        "restore": [{"folder": str(f), "folder_name": f.name} for f in unreferenced_matched],
        "archive_corpses": corpses,
        "blocking_active_only_with_assets": blocking_with_assets,
        "unreferenced_not_matched": [{"folder": str(f), "folder_name": f.name} for f in unreferenced_only],
        "paper_pollution_repairs": repairs,
        "raw_internal_duplicates": raw_internal_dups,
    }


def _new_transaction_dir(paper_raw_dir: Path) -> Path:
    base = paper_raw_dir.parent / "transactions" / f"reconcile_paper_raw_{now_iso().replace(':', '').replace('-', '')}"
    candidate = base
    i = 1
    while candidate.exists():
        i += 1
        candidate = Path(f"{base}_{i}")
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def _apply_plan(plan: dict[str, Any], *, paper_raw_dir: Path, reason: str) -> dict[str, Any]:
    tx_dir = _new_transaction_dir(paper_raw_dir)
    corpse_dir = tx_dir / "empty_corpses"
    corpse_dir.mkdir(parents=True, exist_ok=True)
    moved_corpses: list[dict[str, str]] = []
    restored: list[dict[str, str]] = []
    repairs_applied: list[dict[str, Any]] = []
    errors: list[str] = []

    # 1. Archive corpses (move, never delete).
    for entry in plan["archive_corpses"]:
        src = Path(entry["folder"])
        if not src.exists():
            continue
        dst = corpse_dir / src.name
        if dst.exists():
            errors.append(f"corpse archive target exists: {dst}")
            continue
        shutil.move(str(src), str(dst))
        moved_corpses.append({"from": str(src), "to": str(dst)})

    # 2. Restore unreferenced-matched workspaces to active root, applying
    #    .paper pollution repairs (pure renames) where planned.
    repair_by_source = {p["source_folder"]: p for p in plan["paper_pollution_repairs"]}
    for entry in plan["restore"]:
        src = Path(entry["folder"])
        if not src.exists():
            errors.append(f"restore source missing: {src}")
            continue
        repair = repair_by_source.get(str(src))
        # Determine the final folder name after any repair.
        if repair:
            # Apply file renames first (inside source), then rename the folder.
            for rename in repair["file_renames"]:
                old = src / rename["from"]
                new = src / rename["to"]
                if old.exists() and not new.exists():
                    old.rename(new)
            final_name = repair["folder_after"]
        else:
            final_name = src.name
        dst = paper_raw_dir / final_name
        if dst.exists():
            errors.append(f"restore target exists in active root: {dst}")
            continue
        # Rename source folder if a repair changed its name, then move to active.
        if repair and src.name != final_name:
            repaired_src = src.parent / final_name
            src.rename(repaired_src)
            src = repaired_src
            repairs_applied.append({"source": entry["folder"], "repaired_to": final_name,
                                    "file_renames": repair["file_renames"]})
        shutil.move(str(src), str(dst))
        restored.append({"from": entry["folder"], "to": str(dst), "final_name": final_name})

    result = {
        "applied": True,
        "transaction_dir": str(tx_dir),
        "reason": reason,
        "applied_at": now_iso(),
        "moved_corpses": moved_corpses,
        "restored": restored,
        "repairs_applied": repairs_applied,
        "errors": errors,
    }
    atomic_write_json(tx_dir / "reconcile_report.json", result, indent=2)
    return result


def _print_human_report(plan: dict[str, Any]) -> None:
    s = plan["summary"]
    print(f"non-destructive reconcile plan — expect {plan['expect_count']} active workspaces")
    print(f"  raw PDFs            : {s['raw_pdf_count']}")
    print(f"  active workspaces   : {s['active_workspace_count']} (matched raw: {s['active_matched_raw_count']})")
    print(f"  unreferenced matched: {s['unreferenced_matched_raw_count']} (to restore)")
    print(f"  unreferenced only   : {s['unreferenced_only_count']} (left in quarantine)")
    print(f"  raw-only (new)      : {s['raw_only_count']}")
    print(f"  active-only         : {s['active_only_count']} "
          f"(corpses={s['active_only_corpse_count']}, with_assets={s['active_only_with_assets_count']})")
    print(f"  expected final active: {s['expected_final_active_count']}")
    print(f"  reconstructable w/o metadata loss: {s['target_reconstructable_without_metadata_loss']}")
    if plan["paper_pollution_repairs"]:
        print(f"  .paper pollution repairs: {len(plan['paper_pollution_repairs'])}")
        for r in plan["paper_pollution_repairs"]:
            print(f"    {r['folder_before']} -> {r['folder_after']} ({len(r['file_renames'])} file rename(s))")
    if plan["errors"]:
        print("  ERRORS:")
        for e in plan["errors"]:
            print(f"    - {e}")
    if plan["blocking_active_only_with_assets"]:
        print("  BLOCKING: active_only workspaces with real assets:")
        for e in plan["blocking_active_only_with_assets"]:
            print(f"    - {e['folder']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Non-destructively reconcile paper_raw to the raw PDF set.")
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--expect-count", type=int, required=True,
                        help="required: exact number of raw PDFs / final active workspaces")
    parser.add_argument("--dry-run", action="store_true", help="force dry-run (default)")
    parser.add_argument("--apply", action="store_true", help="execute the restore + archive")
    parser.add_argument("--i-understand-this-moves-existing-workspaces", action="store_true")
    parser.add_argument("--reason", default="")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args(argv)

    write = bool(args.apply and not args.dry_run)
    if write and not args.i_understand_this_moves_existing_workspaces:
        parser.error("--apply requires --i-understand-this-moves-existing-workspaces")
    if write and not args.reason.strip():
        parser.error("--apply requires --reason")

    plan = build_plan(
        raw_dir=args.raw_dir,
        paper_raw_dir=args.paper_raw_dir,
        papers_dir=args.papers_dir,
        expect_count=args.expect_count,
    )

    # Refuse to apply if the plan is not clean.
    if write and (plan["errors"] or not plan["summary"]["target_reconstructable_without_metadata_loss"]):
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        print("REFUSING --apply: plan is not reconstructable without metadata loss or has errors:", file=sys.stderr)
        _print_human_report(plan)
        return 1

    result: dict[str, Any]
    if write:
        result = _apply_plan(plan, paper_raw_dir=args.paper_raw_dir, reason=args.reason)
        # Post-verify: active count must equal expect_count.
        active_after = _active_workspace_folders(args.paper_raw_dir)
        result["post_active_workspace_count"] = len(active_after)
        result["expect_count"] = args.expect_count
        result["count_ok"] = len(active_after) == args.expect_count
        report = {"plan": plan, "apply_result": result}
    else:
        report = plan

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    _print_human_report(plan)
    if write:
        print(f"\nAPPLY: restored={len(result['restored'])} corpses_archived={len(result['moved_corpses'])} "
              f"repairs={len(result['repairs_applied'])} active_after={result['post_active_workspace_count']} "
              f"count_ok={result['count_ok']}")
        if result["errors"]:
            print("APPLY ERRORS:", file=sys.stderr)
            for e in result["errors"]:
                print(f"  - {e}", file=sys.stderr)

    if plan["errors"]:
        return 1
    if write and (result["errors"] or not result["count_ok"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
