"""Reconcile ``data/raw`` PDFs against ``data/paper_raw`` workspaces.

This audit answers one question: *can the 187 raw PDFs be reconstructed as
187 active paper_raw workspaces without rebuilding metadata?* The ingest
duplicate guard only blocks a raw PDF whose content already lives in
paper_raw/papers; it does NOT stop a raw queue from being stacked on top of
pre-existing workspaces whose source PDF is no longer in ``data/raw``, nor does
it look inside ``quarantine/`` (so a raw PDF whose workspace was quarantined as
"unreferenced" would be re-staged as a brand-new workspace).

The audit hashes five PDF sets and joins them by sha256/md5:

* ``data/raw/*.pdf``                                  — the incoming queue (source of truth)
* ``data/paper_raw/*/*.pdf``                          — active root workspaces (excludes quarantine)
* ``data/paper_raw/quarantine/unreferenced_workspaces/**/*.pdf``
* ``data/paper_raw/quarantine/duplicate_workspaces/**/*.pdf``
* ``data/papers/*/*.pdf``                             — the formal committed library

Default mode is read-only and side-effect free. ``--expect-final-count N``
fails the audit unless the raw queue can collapse to exactly ``N`` active
workspaces. It never writes to paper_raw, papers, the ledger, or quarantine.

See ``docs/PROJECT_CONTRACT.md`` for the data boundary: ``data/raw`` is a queue,
``data/paper_raw`` is the workspace, ``data/papers`` is the committed library.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import PAPER_NUMBER_LEDGER_PATH, PAPER_RAW_DIR, PAPERS_DIR, RAW_DIR
from src.file_fingerprint import compute_file_hashes
from src.services.ingest_duplicate_guard import (
    find_best_pdf,
    is_paper_raw_workspace,
    resolve_paper_raw_identity,
)
from src.services.ingest_state import now_iso
from src.services.v2_library import PaperNumberLedger


# Files that may live in an empty corpse workspace without counting as "real
# assets". Anything else (pdf/metadata/md/catalog/non-empty images) means the
# workspace carries real work and must NOT be auto-archived.
_CORPSE_ALLOWED_FILES = {".import_status.json", ".DS_Store", "Thumbs.db"}


@dataclass
class PdfRecord:
    path: Path
    md5: str
    sha256: str
    file_size: int
    workspace: str = ""        # parent folder name, "" for raw/papers roots
    paper_number: str = ""
    scope: str = ""            # raw | active_paper_raw | unreferenced | duplicate | papers

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "md5": self.md5,
            "sha256": self.sha256,
            "file_size": self.file_size,
            "workspace": self.workspace,
            "paper_number": self.paper_number,
            "scope": self.scope,
        }


def _hash_pdf(path: Path) -> PdfRecord | None:
    try:
        hashes = compute_file_hashes(path)
    except OSError:
        return None
    return PdfRecord(
        path=path,
        md5=str(hashes["md5"]).lower(),
        sha256=str(hashes["sha256"]).lower(),
        file_size=int(hashes["file_size"]),
    )


def _scan_raw(raw_dir: Path) -> list[PdfRecord]:
    if not raw_dir.exists():
        return []
    out: list[PdfRecord] = []
    for pdf in sorted(p for p in raw_dir.glob("*.pdf") if p.is_file()):
        rec = _hash_pdf(pdf)
        if rec is not None:
            rec.scope = "raw"
            out.append(rec)
    return out


def _scan_active_paper_raw(paper_raw_dir: Path) -> list[PdfRecord]:
    """Hash the primary PDF of every active root workspace (excludes quarantine)."""
    if not paper_raw_dir.exists():
        return []
    out: list[PdfRecord] = []
    for folder in sorted(p for p in paper_raw_dir.iterdir() if p.is_dir()):
        if folder.name == "quarantine" or folder.name.startswith("."):
            continue
        if not is_paper_raw_workspace(folder):
            continue
        pdf = find_best_pdf(folder)
        if pdf is None:
            continue
        rec = _hash_pdf(pdf)
        if rec is None:
            continue
        paper_number, _ = resolve_paper_raw_identity(folder)
        rec.workspace = folder.name
        rec.paper_number = paper_number
        rec.scope = "active_paper_raw"
        out.append(rec)
    return out


def _scan_quarantine_subdir(holder: Path, scope: str) -> list[PdfRecord]:
    if not holder.exists():
        return []
    out: list[PdfRecord] = []
    for pdf in sorted(holder.rglob("*.pdf")):
        if not pdf.is_file():
            continue
        rec = _hash_pdf(pdf)
        if rec is None:
            continue
        rec.workspace = pdf.parent.name
        rec.scope = scope
        out.append(rec)
    return out


def _scan_papers(papers_dir: Path) -> list[PdfRecord]:
    if not papers_dir.exists():
        return []
    out: list[PdfRecord] = []
    for folder in sorted(p for p in papers_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        pdf = find_best_pdf(folder)
        if pdf is None:
            continue
        rec = _hash_pdf(pdf)
        if rec is None:
            continue
        rec.workspace = folder.name
        rec.scope = "papers"
        out.append(rec)
    return out


def _duplicates_within(records: list[PdfRecord]) -> list[dict[str, Any]]:
    by_sha: dict[str, list[PdfRecord]] = defaultdict(list)
    for rec in records:
        by_sha[rec.sha256].append(rec)
    groups: list[dict[str, Any]] = []
    for sha, members in sorted(by_sha.items()):
        if len(members) < 2:
            continue
        groups.append({
            "sha256": sha,
            "scope": members[0].scope,
            "members": [m.to_dict() for m in members],
        })
    return groups


def _is_empty_corpse(folder: Path) -> bool:
    """True if an active workspace carries no real assets (safe to archive).

    A corpse may contain only ``.import_status.json``, ``*.paper.number``
    markers, system noise (``.DS_Store``/``Thumbs.db``), and empty directories.
    Any PDF / metadata / markdown / catalog / non-empty images dir means real
    work is present and the workspace must NOT be auto-archived.
    """
    for pattern in ("*.pdf", "*.metadata.json", "*.md", "*.catalog.json"):
        if any(folder.glob(pattern)):
            return False
    images = folder / "images"
    if images.exists() and any(p for p in images.rglob("*") if p.is_file()):
        return False
    for child in folder.iterdir():
        if child.is_dir():
            if child.name == "images":
                continue  # handled above (must be empty/absent)
            if any(p for p in child.rglob("*") if p.is_file()):
                return False
            continue
        if child.name in _CORPSE_ALLOWED_FILES:
            continue
        if child.name.endswith(".paper.number"):
            continue
        return False
    return True


def _active_workspace_folders(paper_raw_dir: Path) -> list[Path]:
    if not paper_raw_dir.exists():
        return []
    return [
        f for f in sorted(paper_raw_dir.iterdir())
        if f.is_dir() and f.name != "quarantine" and not f.name.startswith(".") and is_paper_raw_workspace(f)
    ]


def _ledger_mismatches(ledger_path: Path) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    if not ledger_path.exists():
        mismatches.append({"kind": "ledger_missing", "path": str(ledger_path)})
        return mismatches
    ledger = PaperNumberLedger(ledger_path).load()
    items = ledger.get("items") or {}
    max_number = str(ledger.get("max_number") or "0000000000000000")

    paper_raw_dir = ledger_path.parent.parent / "paper_raw"
    active_numbers: set[str] = set()
    for folder in _active_workspace_folders(paper_raw_dir):
        number, _ = resolve_paper_raw_identity(folder)
        if number:
            active_numbers.add(number)

    ledger_numbers = set(items)
    missing_in_ledger = sorted(active_numbers - ledger_numbers)
    orphan_ledger = sorted(ledger_numbers - active_numbers)
    if missing_in_ledger:
        mismatches.append({"kind": "active_workspaces_missing_from_ledger", "count": len(missing_in_ledger), "paper_numbers": missing_in_ledger[:20]})
    if orphan_ledger:
        quarantined = [n for n in orphan_ledger if str((items.get(n) or {}).get("state") or "").startswith("quarantined")]
        real_orphans = [n for n in orphan_ledger if n not in quarantined]
        if real_orphans:
            mismatches.append({"kind": "ledger_items_without_active_workspace", "count": len(real_orphans), "paper_numbers": real_orphans[:20]})

    expected_max = f"{len(active_numbers):016d}" if active_numbers else "0000000000000000"
    if max_number != expected_max:
        mismatches.append({
            "kind": "max_number_mismatch",
            "ledger_max_number": max_number,
            "expected_from_active_count": expected_max,
            "active_workspace_count": len(active_numbers),
        })
    return mismatches


def build_report(
    *,
    raw_dir: Path,
    paper_raw_dir: Path,
    papers_dir: Path,
    ledger_path: Path,
    expect_final_count: int | None = None,
) -> dict[str, Any]:
    raw_records = _scan_raw(raw_dir)
    active_records = _scan_active_paper_raw(paper_raw_dir)
    unreferenced_records = _scan_quarantine_subdir(
        paper_raw_dir / "quarantine" / "unreferenced_workspaces", "unreferenced"
    )
    duplicate_records = _scan_quarantine_subdir(
        paper_raw_dir / "quarantine" / "duplicate_workspaces", "duplicate"
    )
    papers_records = _scan_papers(papers_dir)

    active_sha = {r.sha256 for r in active_records}
    active_md5 = {r.md5 for r in active_records}
    unref_sha = {r.sha256 for r in unreferenced_records}
    unref_md5 = {r.md5 for r in unreferenced_records}

    raw_matched_active: list[PdfRecord] = []
    raw_matched_unreferenced: list[PdfRecord] = []
    raw_only: list[PdfRecord] = []
    for rec in raw_records:
        if rec.sha256 in active_sha or rec.md5 in active_md5:
            raw_matched_active.append(rec)
        elif rec.sha256 in unref_sha or rec.md5 in unref_md5:
            raw_matched_unreferenced.append(rec)
        else:
            raw_only.append(rec)

    raw_shas = {r.sha256 for r in raw_records}
    raw_md5s = {r.md5 for r in raw_records}

    # active_only = active workspaces NOT matched to raw. This includes both
    # PDF-bearing workspaces whose PDF is absent from raw AND PDF-less corpses.
    active_with_pdf_sha = {r.sha256 for r in active_records}
    active_folders = _active_workspace_folders(paper_raw_dir)
    active_matched_folders = {
        r.workspace for r in active_records if r.sha256 in raw_shas or r.md5 in raw_md5s
    }
    active_only_folders: list[Path] = []
    active_only_with_assets: list[dict[str, Any]] = []
    active_only_corpses: list[dict[str, Any]] = []
    for folder in active_folders:
        if folder.name in active_matched_folders:
            continue
        active_only_folders.append(folder)
        corpse = _is_empty_corpse(folder)
        entry = {"folder": str(folder), "folder_name": folder.name, "is_empty_corpse": corpse}
        (active_only_corpses if corpse else active_only_with_assets).append(entry)

    active_workspace_count = len(active_folders)
    active_matched_raw_count = len(active_matched_folders)
    unreferenced_matched_raw_count = len(raw_matched_unreferenced)
    active_only_count = len(active_only_folders)

    ledger = PaperNumberLedger(ledger_path).load() if ledger_path.exists() else PaperNumberLedger.empty_data()
    ledger_max_number = str(ledger.get("max_number") or "0000000000000000")
    ledger_item_count = len(ledger.get("items") or {})

    # The 187 raw PDFs are reconstructable without metadata loss when:
    #   * every raw PDF is already an active or unreferenced workspace (raw_only=0)
    #   * the active+unreferenced matches cover the full raw set
    #   * every active_only workspace is a confirmed empty corpse (no real assets)
    target_reconstructable = (
        len(raw_only) == 0
        and (active_matched_raw_count + unreferenced_matched_raw_count) == len(raw_records)
        and len(active_only_with_assets) == 0
    )

    # Incremental staging is unsafe when there are new PDFs to stage AND the
    # existing paper_raw already holds workspaces not backed by a raw PDF.
    safe_to_stage_incrementally = not (
        len(raw_only) > 0 and active_workspace_count > active_matched_raw_count
    )

    expected_final = active_workspace_count + len(raw_only)
    count_consistent = True
    expect_errors: list[str] = []
    if expect_final_count is not None:
        if len(raw_records) != expect_final_count:
            count_consistent = False
            expect_errors.append(
                f"raw_pdf_count={len(raw_records)} != expect_final_count={expect_final_count}"
            )
        if expected_final != expect_final_count:
            count_consistent = False
            expect_errors.append(
                f"active_workspace_count({active_workspace_count}) + raw_only_count({len(raw_only)})"
                f" = {expected_final} != expect_final_count={expect_final_count})"
            )

    summary: dict[str, Any] = {
        "raw_pdf_count": len(raw_records),
        "active_paper_raw_workspace_count": active_workspace_count,
        "active_paper_raw_pdf_count": len(active_records),
        "active_matched_raw_count": active_matched_raw_count,
        "unreferenced_matched_raw_count": unreferenced_matched_raw_count,
        "raw_matched_active_count": len(raw_matched_active),  # back-compat alias
        "raw_matched_unreferenced_count": len(raw_matched_unreferenced),
        "raw_only_count": len(raw_only),
        "active_only_count": active_only_count,
        "active_only_corpse_count": len(active_only_corpses),
        "active_only_with_assets_count": len(active_only_with_assets),
        "duplicate_quarantine_pdf_count": len(duplicate_records),
        "papers_dir_pdf_count": len(papers_records),
        "ledger_max_number": ledger_max_number,
        "ledger_item_count": ledger_item_count,
        "expected_final_count_if_incremental": expected_final,
        "expected_final_count": expect_final_count,
        "safe_to_stage_incrementally": safe_to_stage_incrementally,
        "target_reconstructable_without_metadata_loss": target_reconstructable,
        "count_consistent": count_consistent,
    }
    return {
        "schema": "raw_vs_paper_raw_audit",
        "generated_at": now_iso(),
        "raw_dir": str(raw_dir),
        "paper_raw_dir": str(paper_raw_dir),
        "papers_dir": str(papers_dir),
        "ledger_path": str(ledger_path),
        "summary": summary,
        "expect_errors": expect_errors,
        "raw_only": [r.to_dict() for r in raw_only],
        "raw_matched_active": [r.to_dict() for r in raw_matched_active],
        "raw_matched_unreferenced": [r.to_dict() for r in raw_matched_unreferenced],
        "active_only_corpses": active_only_corpses,
        "active_only_with_assets": active_only_with_assets,
        "duplicates_inside_raw": _duplicates_within(raw_records),
        "duplicates_inside_active_paper_raw": _duplicates_within(active_records),
        "ledger_mismatches": _ledger_mismatches(ledger_path),
    }


def _print_human_report(report: dict[str, Any]) -> None:
    s = report["summary"]
    print(f"raw vs paper_raw audit — {s['raw_pdf_count']} raw PDF(s), "
          f"{s['active_paper_raw_workspace_count']} active workspace(s), "
          f"{s['active_paper_raw_pdf_count']} with PDF")
    print(f"  active matched raw  : {s['active_matched_raw_count']}")
    print(f"  unreferenced match  : {s['unreferenced_matched_raw_count']}")
    print(f"  raw-only (new)      : {s['raw_only_count']}")
    print(f"  active-only (no raw): {s['active_only_count']} "
          f"(corpses={s['active_only_corpse_count']}, with_assets={s['active_only_with_assets_count']})")
    print(f"  duplicate quarantine: {s['duplicate_quarantine_pdf_count']}")
    print(f"  papers PDFs         : {s['papers_dir_pdf_count']}")
    print(f"  ledger              : max={s['ledger_max_number']} items={s['ledger_item_count']}")
    print(f"  reconstructable w/o metadata loss: {s['target_reconstructable_without_metadata_loss']}")
    print(f"  safe_to_stage_incremental         : {s['safe_to_stage_incrementally']}"
          f"  (incremental final={s['expected_final_count_if_incremental']})")
    if s.get("expected_final_count") is not None:
        print(f"  expect_final_count  : {s['expected_final_count']}  count_consistent={s['count_consistent']}")
    if report.get("expect_errors"):
        print("  EXPECT ERRORS:")
        for err in report["expect_errors"]:
            print(f"    - {err}")
    if report.get("ledger_mismatches"):
        print("  LEDGER MISMATCHES:")
        for m in report["ledger_mismatches"]:
            print(f"    - {m}")
    if report.get("active_only_with_assets"):
        print("  BLOCKING: active_only workspaces with real assets (will NOT auto-archive):")
        for e in report["active_only_with_assets"]:
            print(f"    - {e['folder']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit data/raw PDFs vs data/paper_raw workspaces.")
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--expect-final-count", type=int, default=None,
                        help="fail unless the raw queue collapses to exactly this many active workspaces")
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON report")
    args = parser.parse_args(argv)

    report = build_report(
        raw_dir=args.raw_dir,
        paper_raw_dir=args.paper_raw_dir,
        papers_dir=args.papers_dir,
        ledger_path=args.ledger_path,
        expect_final_count=args.expect_final_count,
    )

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human_report(report)

    if args.expect_final_count is not None and not report["summary"]["count_consistent"]:
        return 1
    if not report["summary"]["safe_to_stage_incrementally"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
