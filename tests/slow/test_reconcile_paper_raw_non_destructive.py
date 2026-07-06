"""Tests for non-destructive paper_raw reconcile + metadata-protecting compact.

Slimmed to core safety behaviors + the unique stage/repair guards that live
here (they share the same helpers). Removed: equal-fixture variants, report
field details, compact numbering details (covered by test_paper_number_admin),
and the bare-rebuild / pollution / papers-nonempty variants that duplicate the
retained core tests.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.services.ingest_duplicate_guard import is_paper_raw_workspace, read_best_metadata_json
from src.services.paper_number_admin import (
    PaperNumberAdminService,
    metadata_fingerprint,
)
from src.services.v2_library import PaperNumberLedger, empty_metadata

_REPO_ROOT = Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0, str(_REPO_ROOT))
import runpy


def _run_script(name: str, argv: list[str]) -> int:
    saved = sys.argv
    sys.argv = argv
    try:
        runpy.run_path(str(_REPO_ROOT / "scripts" / name), run_name="__main__")
        return 0
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = saved


def _hash_tree(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for p in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = p.relative_to(root).as_posix()
        out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _ledger_path(root: Path) -> Path:
    return root / "catalog" / "paper_number_ledger.json"


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _pdf_bytes(label: str) -> bytes:
    return f"%PDF {label}".encode()


def _make_numbered_workspace(root: Path, number: str, pdf_bytes: bytes, *, status: str = "converted") -> Path:
    """Active numbered workspace with PDF + metadata + marker + ledger entry."""
    folder = root / "paper_raw" / number
    folder.mkdir(parents=True)
    (folder / f"{number}.pdf").write_bytes(pdf_bytes)
    meta = empty_metadata(number)
    meta["title"]["original"] = f"Paper {number}"
    meta["year"] = 2020
    meta["authors"] = [{"full_name": "Test Author", "family": "Author", "given": "T", "orcid": "", "affiliation": ""}]
    meta["first_author"] = {"family": "Author", "display": "Test Author"}
    meta["container"]["journal"] = "Test Journal"
    _write_json(folder / f"{number}.metadata.json", meta)
    (folder / f"{number}.md").write_text(f"# Paper {number}\nbody", encoding="utf-8")
    (folder / "images").mkdir()
    _write_json(folder / f"{number}.conversion.json", {"status": "converted", "paper_number": number})
    _write_json(folder / ".import_status.json", {"status": status, "paper_number": number})
    PaperNumberLedger(_ledger_path(root)).reserve_specific_for_paper_raw(number, folder)
    return folder


def _make_named_unreferenced(root: Path, folder_name: str, paper_number: str, pdf_bytes: bytes) -> Path:
    """A legacy/untitled workspace sitting in quarantine/unreferenced_workspaces."""
    folder = root / "paper_raw" / "quarantine" / "unreferenced_workspaces" / folder_name
    folder.mkdir(parents=True)
    (folder / f"{folder_name}.pdf").write_bytes(pdf_bytes)
    meta = empty_metadata(folder_name)
    meta["paper_number"] = paper_number
    meta["paper_raw_id"] = paper_number
    meta["title"]["original"] = f"Legacy {folder_name}"
    _write_json(folder / f"{folder_name}.metadata.json", meta)
    (folder / f"{folder_name}.md").write_text(f"# Legacy {folder_name}", encoding="utf-8")
    (folder / "images").mkdir()
    (folder / f"{paper_number}.paper.number").write_text(
        json.dumps({"paper_number": paper_number, "folder_name": folder_name, "state": "reserved"}), encoding="utf-8")
    _write_json(folder / ".import_status.json", {"status": "metadata_resolve_failed", "paper_number": paper_number})
    return folder


def _make_corpse(root: Path, number: str) -> Path:
    folder = root / "paper_raw" / number
    folder.mkdir(parents=True)
    _write_json(folder / ".import_status.json", {"status": "metadata_invalid", "paper_number": number})
    (folder / f"{number}.paper.number").write_text(
        json.dumps({"paper_number": number, "folder_name": number, "state": "reserved"}), encoding="utf-8")
    PaperNumberLedger(_ledger_path(root)).reserve_specific_for_paper_raw(number, folder)
    return folder


def _setup_raw(root: Path, labels: list[str]) -> dict[str, Path]:
    raw = root / "raw"
    raw.mkdir(parents=True)
    out: dict[str, Path] = {}
    for label in labels:
        p = raw / f"{label}.pdf"
        p.write_bytes(_pdf_bytes(label))
        out[label] = p
    return out


def _service(root: Path) -> PaperNumberAdminService:
    return PaperNumberAdminService(
        paper_raw_dir=root / "paper_raw",
        papers_dir=root / "papers",
        ledger_path=_ledger_path(root),
        transactions_dir=root / "transactions",
    )


# ── Core reconcile safety behaviors ───────────────────────────────────

def test_reconcile_restores_unreferenced_and_archives_corpse(tmp_path):
    root = tmp_path / "proj"
    _setup_raw(root, ["a", "b", "c", "d"])
    _make_numbered_workspace(root, "0000000000000001", _pdf_bytes("a"))
    _make_numbered_workspace(root, "0000000000000002", _pdf_bytes("b"))
    _make_corpse(root, "0000000000000003")  # corpse, no raw match
    _make_named_unreferenced(root, "1990_legacy_one", "0000000000000010", _pdf_bytes("c"))
    _make_named_unreferenced(root, "1991_legacy_two", "0000000000000011", _pdf_bytes("d"))
    (root / "papers").mkdir(parents=True)

    rc = _run_script("reconcile_paper_raw_non_destructive.py", [
        "reconcile_paper_raw_non_destructive.py",
        "--raw-dir", str(root / "raw"),
        "--paper-raw-dir", str(root / "paper_raw"),
        "--papers-dir", str(root / "papers"),
        "--expect-count", "4",
        "--apply",
        "--i-understand-this-moves-existing-workspaces",
        "--reason", "test restore",
    ])
    assert rc == 0
    active = [f for f in (root / "paper_raw").iterdir()
              if f.is_dir() and f.name != "quarantine" and not f.name.startswith(".") and is_paper_raw_workspace(f)]
    assert len(active) == 4
    # corpse archived, not deleted
    corpses_dir = list((root / "transactions").rglob("empty_corpses"))
    assert corpses_dir
    assert (corpses_dir[0] / "0000000000000003").is_dir()
    # unreferenced restored to active
    assert (root / "paper_raw" / "1990_legacy_one").is_dir()
    assert (root / "paper_raw" / "1991_legacy_two").is_dir()


def test_reconcile_metadata_bytes_unchanged(tmp_path):
    root = tmp_path / "proj"
    _setup_raw(root, ["a", "b"])
    _make_numbered_workspace(root, "0000000000000001", _pdf_bytes("a"))
    unref = _make_named_unreferenced(root, "1990_legacy", "0000000000000010", _pdf_bytes("b"))
    (root / "papers").mkdir(parents=True)
    meta_path = unref / "1990_legacy.metadata.json"
    before = meta_path.read_bytes()

    rc = _run_script("reconcile_paper_raw_non_destructive.py", [
        "reconcile_paper_raw_non_destructive.py",
        "--raw-dir", str(root / "raw"),
        "--paper-raw-dir", str(root / "paper_raw"),
        "--papers-dir", str(root / "papers"),
        "--expect-count", "2",
        "--apply",
        "--i-understand-this-moves-existing-workspaces",
        "--reason", "test",
    ])
    assert rc == 0
    after = (root / "paper_raw" / "1990_legacy" / "1990_legacy.metadata.json").read_bytes()
    assert before == after, "metadata file bytes must be unchanged by reconcile"


def test_reconcile_active_only_with_assets_blocks(tmp_path):
    """Dangerous case: an active workspace with real assets not in raw must block apply."""
    root = tmp_path / "proj"
    _setup_raw(root, ["a"])
    _make_numbered_workspace(root, "0000000000000001", _pdf_bytes("NOT_IN_RAW"))
    (root / "papers").mkdir(parents=True)

    rc = _run_script("reconcile_paper_raw_non_destructive.py", [
        "reconcile_paper_raw_non_destructive.py",
        "--raw-dir", str(root / "raw"),
        "--paper-raw-dir", str(root / "paper_raw"),
        "--papers-dir", str(root / "papers"),
        "--expect-count", "1",
        "--apply",
        "--i-understand-this-moves-existing-workspaces",
        "--reason", "test",
    ])
    assert rc == 1, "active_only with real assets must block apply"


def test_dry_run_changes_no_file_hashes(tmp_path):
    root = tmp_path / "proj"
    _setup_raw(root, ["a", "b"])
    _make_numbered_workspace(root, "0000000000000001", _pdf_bytes("a"))
    _make_named_unreferenced(root, "1990_legacy", "0000000000000010", _pdf_bytes("b"))
    (root / "papers").mkdir(parents=True)
    before = _hash_tree(root / "paper_raw")
    before_ledger = _hash_tree(root / "catalog")

    rc = _run_script("reconcile_paper_raw_non_destructive.py", [
        "reconcile_paper_raw_non_destructive.py",
        "--raw-dir", str(root / "raw"),
        "--paper-raw-dir", str(root / "paper_raw"),
        "--papers-dir", str(root / "papers"),
        "--expect-count", "2",
        "--dry-run",
    ])
    assert rc == 0
    assert _hash_tree(root / "paper_raw") == before
    assert _hash_tree(root / "catalog") == before_ledger


# ── Core compact safety behavior ──────────────────────────────────────

def test_compact_preserves_metadata_fingerprint(tmp_path):
    root = tmp_path / "proj"
    _setup_raw(root, ["a", "b"])
    f1 = _make_numbered_workspace(root, "0000000000000100", _pdf_bytes("a"))
    f2 = _make_numbered_workspace(root, "0000000000000102", _pdf_bytes("b"))
    # install ledger entries for the two numbered workspaces
    _write_json(_ledger_path(root), {
        "schema_version": "1.0", "max_number": "0000000000000102",
        "items": {
            "0000000000000100": {"folder_name": "0000000000000100", "folder_path": str(f1),
                                 "planned_paper_id": "", "state": "reserved", "created_at": "2026-01-01"},
            "0000000000000102": {"folder_name": "0000000000000102", "folder_path": str(f2),
                                 "planned_paper_id": "", "state": "reserved", "created_at": "2026-01-01"},
        }})

    fp_before = {
        "0000000000000100": metadata_fingerprint(read_best_metadata_json(f1)),
        "0000000000000102": metadata_fingerprint(read_best_metadata_json(f2)),
    }
    svc = _service(root)
    report = svc.compact_paper_raw(apply=True, reason="test", sort="old-number", protect_metadata=True)
    assert report["applied"] is True
    assert not report["errors"]
    # after compact, folders are renumbered to 0001, 0002
    a = root / "paper_raw" / "0000000000000001"
    b = root / "paper_raw" / "0000000000000002"
    assert a.is_dir() and b.is_dir()
    fp_after = {
        "0000000000000100": metadata_fingerprint(read_best_metadata_json(a)),
        "0000000000000102": metadata_fingerprint(read_best_metadata_json(b)),
    }
    assert fp_before == fp_after, "normalized metadata fingerprint must be unchanged by compact"


# ── Unique stage guard (not covered by tests/integration/test_stage_raw_pdfs.py) ──

def test_stage_expect_final_count_refuse_on_mismatch(tmp_path):
    root = tmp_path / "proj"
    _setup_raw(root, ["a", "b"])
    # one active workspace already present (not matching raw)
    _make_numbered_workspace(root, "0000000000000001", _pdf_bytes("EXISTING"))
    rc = _run_script("stage_raw_pdfs_to_paper_raw.py", [
        "stage_raw_pdfs_to_paper_raw.py",
        "--raw-dir", str(root / "raw"),
        "--paper-raw-dir", str(root / "paper_raw"),
        "--papers-dir", str(root / "papers"),
        "--ledger-path", str(_ledger_path(root)),
        "--apply", "--move",
        "--expect-final-count", "1",
        "--refuse-if-final-count-mismatch",
    ])
    # active=1 + planned_new=2 = 3 != 1 -> refuse
    assert rc == 1
    # nothing staged (refused before staging? the guard refuses apply write)
    # The two raw PDFs are not in paper_raw as new workspaces
    assert not (root / "paper_raw" / "0000000000000002").exists()


def test_stage_dry_run_numbers_only_for_non_duplicates(tmp_path):
    root = tmp_path / "proj"
    raw = root / "raw"
    raw.mkdir(parents=True)
    (raw / "a.pdf").write_bytes(_pdf_bytes("same"))
    (raw / "b.pdf").write_bytes(_pdf_bytes("same"))  # batch duplicate of a
    (raw / "c.pdf").write_bytes(_pdf_bytes("unique"))
    report_path = root / "stage_report.json"
    rc = _run_script("stage_raw_pdfs_to_paper_raw.py", [
        "stage_raw_pdfs_to_paper_raw.py",
        "--raw-dir", str(raw),
        "--paper-raw-dir", str(root / "paper_raw"),
        "--papers-dir", str(root / "papers"),
        "--ledger-path", str(_ledger_path(root)),
        "--dry-run", "--move",
        "--report", str(report_path),
    ])
    assert rc == 1  # duplicates present
    items = json.loads(report_path.read_text(encoding="utf-8"))
    planned = [i for i in items if i["status"] == "planned"]
    duplicates = [i for i in items if i["status"] == "duplicate"]
    assert len(planned) == 2  # a and c
    assert len(duplicates) == 1  # b
    # planned numbers are 0001 and 0002 (only non-duplicates counted)
    planned_nums = sorted(i["planned_paper_number"] for i in planned)
    assert planned_nums == ["0000000000000001", "0000000000000002"]


# ── Unique repair_paper_raw_derived_files guards ──────────────────────

def test_repair_derived_files_fixes_stale_refs_and_preserves_metadata(tmp_path):
    """repair_paper_raw_derived_files rebuilds asset_manifest, fixes derived-file
    stale 16-digit tokens, and only changes metadata paper_number/paper_raw_id
    (fingerprint-verified)."""
    root = tmp_path / "proj"
    folder = root / "paper_raw" / "0000000000000018"
    folder.mkdir(parents=True)
    # actual files correctly prefixed 0018
    (folder / "0000000000000018.pdf").write_bytes(_pdf_bytes("paper18"))
    (folder / "0000000000000018.md").write_text("# body 0000000000000018", encoding="utf-8")
    (folder / "images").mkdir()
    meta = empty_metadata("0000000000000018")
    meta["title"]["original"] = "Stale Ref Paper"
    meta["year"] = 2020
    meta["authors"] = [{"full_name": "Author", "family": "Author", "given": "A", "orcid": "", "affiliation": ""}]
    meta["first_author"] = {"family": "Author", "display": "Author"}
    meta["container"]["journal"] = "J"
    _write_json(folder / "0000000000000018.metadata.json", meta)
    _write_json(folder / "0000000000000018.catalog.json", {
        "schema_version": "3.1",
        "library_locator": {
            "paper_number": "0000000000000018",
            "paper_id": "",
            "paper_dir": "",
            "asset_refs": {
                "markdown": "0000000000000017.md",
                "pdf": "0000000000000017.pdf",
                "metadata": "",
                "catalog": "",
                "asset_manifest": "",
                "images_dir": ""
            }
        },
        "provenance": {"markdown_path": "0000000000000017.md"},
    })
    # stale asset_manifest + conversion + import_status referencing 0017
    _write_json(folder / "0000000000000018.asset_manifest.json", {
        "schema_version": "1.0", "paper_number": "0000000000000017", "paper_id": "",
        "stage": "paper_raw", "files": {"pdf": {"path": "0000000000000017.pdf"}},
    })
    _write_json(folder / "0000000000000018.conversion.json", {
        "status": "converted", "paper_number": "0000000000000017",
        "paper_raw_id": "0000000000000017", "markdown_path": "0000000000000017.md",
    })
    _write_json(folder / ".import_status.json", {
        "status": "converted", "paper_number": "0000000000000017",
        "paper_raw_id": "0000000000000017",
    })
    (folder / "0000000000000018.paper.number").write_text(
        json.dumps({"paper_number": "0000000000000018", "folder_name": "0000000000000018", "state": "reserved"}), encoding="utf-8")

    fp_before = metadata_fingerprint(read_best_metadata_json(folder))

    rc = _run_script("repair_paper_raw_derived_files.py", [
        "repair_paper_raw_derived_files.py",
        "--paper-raw-dir", str(root / "paper_raw"),
        "--apply",
    ])
    assert rc == 0

    # asset_manifest rebuilt with correct number
    am = json.loads((folder / "0000000000000018.asset_manifest.json").read_text(encoding="utf-8"))
    assert am["paper_number"] == "0000000000000018"
    assert am["files"]["pdf"]["path"] == "0000000000000018.pdf"
    # conversion fixed
    conv = json.loads((folder / "0000000000000018.conversion.json").read_text(encoding="utf-8"))
    assert conv["paper_number"] == "0000000000000018"
    assert conv["markdown_path"] == "0000000000000018.md"
    # import_status fixed
    st = json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))
    assert st["paper_number"] == "0000000000000018"
    # catalog asset_refs canonicalized
    cat = json.loads((folder / "0000000000000018.catalog.json").read_text(encoding="utf-8"))
    assert cat["library_locator"]["asset_refs"]["markdown"] == "0000000000000018.md"
    assert cat["library_locator"]["asset_refs"]["pdf"] == "0000000000000018.pdf"
    # metadata fingerprint unchanged (only paper_number/paper_raw_id touched)
    fp_after = metadata_fingerprint(read_best_metadata_json(folder))
    assert fp_before == fp_after


def test_repair_derived_files_dry_run_is_readonly(tmp_path):
    root = tmp_path / "proj"
    folder = root / "paper_raw" / "0000000000000018"
    folder.mkdir(parents=True)
    (folder / "0000000000000018.pdf").write_bytes(_pdf_bytes("paper18"))
    (folder / "0000000000000018.md").write_text("# body", encoding="utf-8")
    _write_json(folder / "0000000000000018.asset_manifest.json", {"paper_number": "0000000000000017", "stage": "paper_raw", "files": {}})
    (folder / "0000000000000018.paper.number").write_text(
        json.dumps({"paper_number": "0000000000000018", "folder_name": "0000000000000018", "state": "reserved"}), encoding="utf-8")
    before = _hash_tree(folder)
    rc = _run_script("repair_paper_raw_derived_files.py", [
        "repair_paper_raw_derived_files.py",
        "--paper-raw-dir", str(root / "paper_raw"),
        "--dry-run",
    ])
    assert rc == 0
    assert _hash_tree(folder) == before, "dry-run must not modify any files"
