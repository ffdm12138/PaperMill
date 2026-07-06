from __future__ import annotations

import hashlib
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from src.services.paper_number_admin import PaperNumberAdminService
from src.services.v2_library import PaperNumberLedger, PaperRawAllocator, empty_catalog
from src.services.network_metadata_staging import stage_network_metadata_records


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _hash_tree(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _ledger_path(root: Path) -> Path:
    return root / "catalog" / "paper_number_ledger.json"


def _service(root: Path) -> PaperNumberAdminService:
    return PaperNumberAdminService(
        paper_raw_dir=root / "paper_raw",
        papers_dir=root / "papers",
        ledger_path=_ledger_path(root),
        transactions_dir=root / "transactions",
    )


def _workspace(root: Path, folder_name: str, number: str, *, year: int | None = None, named: bool = False) -> Path:
    folder = root / "paper_raw" / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    _write_json(folder / f"{number}.paper.number", {
        "paper_number": number,
        "folder_name": folder_name,
        "state": "reserved",
        "planned_paper_id": "2024_test_paper" if named else "",
    })
    _write_json(folder / f"{number}.metadata.json", {
        "schema_version": "2.0",
        "paper_number": number,
        "paper_raw_id": number,
        "year": year,
        "title": {"original": "Test"},
        "identifiers": {"doi": ""},
        "metadata_match": {"status": "unmatched"},
    })
    _write_json(folder / f"{number}.asset_manifest.json", {
        "paper_number": number,
        "refs": {"markdown": f"{number}.md"},
    })
    _write_json(folder / f"{number}.conversion.json", {
        "paper_number": number,
        "paper_raw_id": number,
        "markdown": f"{number}.md",
    })
    _write_json(folder / ".import_status.json", {
        "status": "converted",
        "paper_number": number,
        "paper_raw_id": number,
    })
    (folder / f"{number}.md").write_text(f"markdown for {number}\n", encoding="utf-8")
    if named:
        catalog = empty_catalog()
        catalog["library_locator"].update({"paper_number": number, "paper_id": folder_name})
        catalog["library_locator"]["paper_dir"] = f"data/paper_raw/{folder_name}"
        catalog["library_locator"]["asset_refs"].update({"markdown": f"{number}.md", "pdf": f"{number}.pdf"})
        catalog["content_identity"].update({
            "content_title_zh": "测试论文",
            "content_title_original": "Test",
            "content_title_original_candidates": ["Test"],
            "content_language": "en",
            "document_type": "article",
        })
        catalog["classification"]["primary_domain"] = "test"
        catalog["terminology"] = [{"term_original": "test", "term_zh": "测试"}]
        catalog["provenance"].update({
            "markdown_path": f"{number}.md",
            "generated_at": "2026-07-03T00:00:00",
            "generator": "test",
        })
        _write_json(folder / f"{number}.catalog.json", catalog)
        _write_json(folder / f"{number}.formalization.json", {
            "paper_number": number,
            "paper_raw_id": number,
            "paper_id": folder_name,
        })
    return folder


def _install_ledger(root: Path, entries: dict[str, Path], *, active: set[str] | None = None) -> None:
    active = active or set()
    items = {}
    for number, folder in entries.items():
        items[number] = {
            "folder_name": folder.name,
            "folder_path": str(folder),
            "planned_paper_id": "",
            "state": "active" if number in active else "reserved",
            "created_at": "2026-07-03T00:00:00",
        }
    _write_json(_ledger_path(root), {
        "schema_version": "1.0",
        "max_number": max(entries) if entries else "0000000000000000",
        "items": items,
    })


def test_normal_allocation_does_not_recycle_deleted_folder(tmp_path: Path):
    raw = tmp_path / "paper_raw"
    ledger = _ledger_path(tmp_path)
    allocator = PaperRawAllocator(raw, ledger_path=ledger, papers_dir=tmp_path / "papers")
    first = allocator.allocate_workspace()
    second = allocator.allocate_workspace()
    third = allocator.allocate_workspace()
    assert [first["paper_number"], second["paper_number"], third["paper_number"]] == [
        "0000000000000001",
        "0000000000000002",
        "0000000000000003",
    ]
    for child in (raw / "0000000000000002").iterdir():
        child.unlink()
    (raw / "0000000000000002").rmdir()
    fourth = allocator.allocate_workspace()
    assert fourth["paper_number"] == "0000000000000004"


def test_reset_and_compact_refuse_when_papers_nonempty(tmp_path: Path):
    (tmp_path / "papers" / "2024_test").mkdir(parents=True)
    svc = _service(tmp_path)
    reset = svc.reset_empty(apply=False, reason="")
    compact = svc.compact_paper_raw(apply=False, reason="", sort="old-number")
    assert reset["errors"]
    assert compact["errors"]
    assert "data/papers is not empty" in reset["errors"][0]


def test_reset_empty_clears_empty_allocator(tmp_path: Path):
    _install_ledger(tmp_path, {})
    svc = _service(tmp_path)
    report = svc.reset_empty(apply=True, reason="empty allocator reset")
    assert report["applied"] is True
    data = json.loads(_ledger_path(tmp_path).read_text(encoding="utf-8"))
    assert data["max_number"] == "0000000000000000"
    assert data["items"] == {}
    assert data["reset_history"][0]["reason"] == "empty allocator reset"


def test_metadata_backup_restore_reports_write_errors(monkeypatch, tmp_path: Path):
    folder = _workspace(tmp_path, "0000000000000100", "0000000000000100", year=2000)
    svc = _service(tmp_path)

    def _boom(path, data):
        raise RuntimeError("disk full")

    monkeypatch.setattr("src.services.paper_number_admin._write_json", _boom)
    errors = svc._restore_metadata_backups(
        {
            "items": [{
                "old_number": "0000000000000100",
                "folder_after": str(folder),
            }]
        },
        {"0000000000000100": {"paper_number": "0000000000000100"}},
    )

    assert len(errors) == 1
    assert "metadata restore failed" in errors[0]
    assert "disk full" in errors[0]


def test_compact_two_numbered_workspaces_rewrites_assets_and_ledger(tmp_path: Path):
    a = _workspace(tmp_path, "0000000000000100", "0000000000000100", year=2000)
    b = _workspace(tmp_path, "0000000000000102", "0000000000000102", year=2001)
    _install_ledger(tmp_path, {"0000000000000100": a, "0000000000000102": b})
    svc = _service(tmp_path)

    report = svc.compact_paper_raw(apply=True, reason="test compact", sort="old-number")

    assert report["applied"] is True
    assert not report["errors"]
    assert (tmp_path / "paper_raw" / "0000000000000001").is_dir()
    assert (tmp_path / "paper_raw" / "0000000000000002").is_dir()
    assert not (tmp_path / "paper_raw" / "0000000000000100").exists()
    for number in ("0000000000000001", "0000000000000002"):
        folder = tmp_path / "paper_raw" / number
        marker = json.loads((folder / f"{number}.paper.number").read_text(encoding="utf-8"))
        metadata = json.loads((folder / f"{number}.metadata.json").read_text(encoding="utf-8"))
        manifest = json.loads((folder / f"{number}.asset_manifest.json").read_text(encoding="utf-8"))
        conversion = json.loads((folder / f"{number}.conversion.json").read_text(encoding="utf-8"))
        status = json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))
        assert marker["paper_number"] == number
        assert marker["folder_name"] == number
        assert metadata["paper_number"] == number
        assert metadata["paper_raw_id"] == number
        assert manifest["paper_number"] == number
        assert conversion["paper_number"] == number
        assert status["paper_number"] == number
    ledger = json.loads(_ledger_path(tmp_path).read_text(encoding="utf-8"))
    assert ledger["schema_version"] == "1.0"
    assert ledger["max_number"] == "0000000000000002"
    assert sorted(ledger["items"]) == ["0000000000000001", "0000000000000002"]


def test_compact_named_workspace_keeps_folder_and_rewrites_catalog_formalization(tmp_path: Path):
    folder = _workspace(
        tmp_path,
        "2024_test_named",
        "0000000000000100",
        year=2024,
        named=True,
    )
    _install_ledger(tmp_path, {"0000000000000100": folder})
    svc = _service(tmp_path)

    report = svc.compact_paper_raw(apply=True, reason="test named compact", sort="old-number")

    assert report["applied"] is True
    assert (tmp_path / "paper_raw" / "2024_test_named").is_dir()
    assert not (folder / "0000000000000100.paper.number").exists()
    marker = json.loads((folder / "0000000000000001.paper.number").read_text(encoding="utf-8"))
    metadata = json.loads((folder / "0000000000000001.metadata.json").read_text(encoding="utf-8"))
    catalog = json.loads((folder / "0000000000000001.catalog.json").read_text(encoding="utf-8"))
    formalization = json.loads((folder / "0000000000000001.formalization.json").read_text(encoding="utf-8"))
    assert marker["paper_number"] == "0000000000000001"
    assert marker["folder_name"] == "2024_test_named"
    assert metadata["paper_number"] == "0000000000000001"
    assert catalog["library_locator"]["paper_number"] == "0000000000000001"
    assert catalog["library_locator"]["asset_refs"]["markdown"] == "0000000000000001.md"
    assert formalization["paper_raw_id"] == "0000000000000001"


def test_marker_parse_does_not_leave_dot_paper_pollution(tmp_path: Path):
    marker = tmp_path / "0000000000000001.paper.number"
    marker.write_text("{}", encoding="utf-8")
    assert PaperNumberLedger.parse_marker_number(marker) == "0000000000000001"
    assert PaperNumberLedger.parse_marker_number(marker) != "0000000000000001.paper"


def test_dry_run_does_not_change_raw_or_ledger_hashes(tmp_path: Path):
    folder = _workspace(tmp_path, "0000000000000100", "0000000000000100", year=2000)
    _install_ledger(tmp_path, {"0000000000000100": folder})
    before_raw = _hash_tree(tmp_path / "paper_raw")
    before_ledger = _hash_tree(tmp_path / "catalog")
    report = _service(tmp_path).compact_paper_raw(apply=False, reason="", sort="old-number")
    assert report["applied"] is False
    assert _hash_tree(tmp_path / "paper_raw") == before_raw
    assert _hash_tree(tmp_path / "catalog") == before_ledger


def test_invalid_corpse_blocks_and_purge_only_removes_empty_invalid(tmp_path: Path):
    good = _workspace(tmp_path, "0000000000000100", "0000000000000100")
    corpse = tmp_path / "paper_raw" / "corpse"
    corpse.mkdir(parents=True)
    _write_json(corpse / ".import_status.json", {"status": "failed"})
    _install_ledger(tmp_path, {"0000000000000100": good})

    blocked = _service(tmp_path).compact_paper_raw(apply=False, reason="", sort="old-number")
    assert blocked["errors"]
    assert corpse.exists()

    applied = _service(tmp_path).compact_paper_raw(
        apply=True,
        reason="purge empty invalid",
        sort="old-number",
        purge_empty_invalid=True,
    )
    assert applied["applied"] is True
    assert not corpse.exists()


def test_quarantine_is_not_compacted(tmp_path: Path):
    good = _workspace(tmp_path, "0000000000000100", "0000000000000100")
    quarantined = _workspace(tmp_path, "quarantine/0000000000000099", "0000000000000099")
    _install_ledger(tmp_path, {"0000000000000100": good})
    report = _service(tmp_path).compact_paper_raw(apply=True, reason="skip quarantine", sort="old-number")
    assert report["applied"] is True
    assert quarantined.exists()
    assert (tmp_path / "paper_raw" / "quarantine" / "0000000000000099" / "0000000000000099.paper.number").exists()


def test_apply_strict_audit_passes_and_next_staging_uses_n_plus_one(tmp_path: Path):
    folder = _workspace(tmp_path, "0000000000000100", "0000000000000100")
    _install_ledger(tmp_path, {"0000000000000100": folder})
    svc = _service(tmp_path)
    report = svc.compact_paper_raw(apply=True, reason="compact before staging", sort="old-number")
    assert report["post_audit"]["ok"] is True

    allocator = PaperRawAllocator(tmp_path / "paper_raw", ledger_path=_ledger_path(tmp_path), papers_dir=tmp_path / "papers")
    next_ws = allocator.allocate_workspace()
    assert next_ws["paper_number"] == "0000000000000002"


def test_allocator_recovers_from_mkdir_before_ledger_orphan(tmp_path: Path):
    raw = tmp_path / "paper_raw"
    orphan = raw / "0000000000001143"
    orphan.mkdir(parents=True)
    _write_json(_ledger_path(tmp_path), {
        "schema_version": "1.0",
        "max_number": "0000000000001142",
        "items": {},
    })

    allocator = PaperRawAllocator(raw, ledger_path=_ledger_path(tmp_path), papers_dir=tmp_path / "papers")
    ws = allocator.allocate_workspace()

    assert ws["paper_number"] == "0000000000001144"
    ledger = json.loads(_ledger_path(tmp_path).read_text(encoding="utf-8"))
    assert ledger["max_number"] == "0000000000001144"
    audit = _service(tmp_path).audit(detect_orphans=True)
    assert any(item["folder"].endswith("0000000000001143") for item in audit["empty_orphan_dirs"])


def test_mark_abandoned_accepts_allocating_state(tmp_path: Path):
    raw = tmp_path / "paper_raw"
    raw.mkdir(parents=True)
    number = "0000000000001143"
    _write_json(_ledger_path(tmp_path), {
        "schema_version": "1.0",
        "max_number": number,
        "items": {
            number: {
                "folder_name": number,
                "folder_path": str(raw / number),
                "planned_paper_id": "",
                "state": "allocating",
                "created_at": "2026-07-07T00:00:00",
            }
        },
    })

    PaperNumberLedger(_ledger_path(tmp_path)).mark_abandoned(number, "test recovery", folder=raw / number)

    ledger = json.loads(_ledger_path(tmp_path).read_text(encoding="utf-8"))
    assert ledger["items"][number]["state"] == "abandoned"
    assert ledger["max_number"] == number


def test_recover_allocating_missing_folder_to_abandoned(tmp_path: Path):
    raw = tmp_path / "paper_raw"
    raw.mkdir(parents=True)
    stale = "0000000000001143"
    _write_json(_ledger_path(tmp_path), {
        "schema_version": "1.0",
        "max_number": stale,
        "items": {
            stale: {
                "folder_name": stale,
                "folder_path": str(raw / stale),
                "planned_paper_id": "",
                "state": "allocating",
                "created_at": "2026-07-07T00:00:00",
            }
        },
    })

    allocator = PaperRawAllocator(raw, ledger_path=_ledger_path(tmp_path), papers_dir=tmp_path / "papers")
    ws = allocator.allocate_workspace()

    assert ws["paper_number"] == "0000000000001144"
    ledger = json.loads(_ledger_path(tmp_path).read_text(encoding="utf-8"))
    assert ledger["items"][stale]["state"] == "abandoned"
    assert ledger["items"]["0000000000001144"]["state"] == "reserved"


def test_allocator_skips_marker_only_reserved_workspace(tmp_path: Path):
    raw = tmp_path / "paper_raw"
    number = "0000000000001143"
    folder = raw / number
    folder.mkdir(parents=True)
    _write_json(folder / f"{number}.paper.number", {
        "paper_number": number,
        "folder_name": number,
        "state": "reserved",
    })
    _write_json(_ledger_path(tmp_path), {
        "schema_version": "1.0",
        "max_number": "0000000000001142",
        "items": {},
    })

    ws = PaperRawAllocator(raw, ledger_path=_ledger_path(tmp_path), papers_dir=tmp_path / "papers").allocate_workspace()

    assert ws["paper_number"] == "0000000000001144"
    audit = _service(tmp_path).audit(detect_orphans=True)
    assert any(item["classification"] == "marker_only_reserved" for item in audit["marker_only_reserved"])


def test_allocator_preserves_metadata_only_workspace_during_reconcile(tmp_path: Path):
    number = "0000000000000143"
    folder = tmp_path / "paper_raw" / number
    folder.mkdir(parents=True)
    _write_json(folder / f"{number}.paper.number", {
        "paper_number": number,
        "folder_name": number,
        "state": "reserved",
    })
    _write_json(folder / f"{number}.metadata.json", {
        "schema_version": "2.0",
        "paper_number": number,
        "paper_raw_id": number,
        "source_type": "network_search",
        "title": {"original": "Metadata only", "subtitle": ""},
        "authors": [{"full_name": "", "family": "", "given": "", "orcid": "", "affiliation": ""}],
        "first_author": {"family": "", "display": ""},
        "year": None,
        "date": {"published": "", "online": "", "accessed": ""},
        "container": {"journal": "", "booktitle": "", "conference": "", "series": "", "publisher": "", "institution": "", "school": ""},
        "publication": {"volume": "", "number": "", "issue": "", "pages": "", "article_number": "", "edition": ""},
        "identifiers": {"doi": "10.1000/metaonly", "arxiv_id": "", "isbn": "", "issn": "", "pmid": "", "pmcid": "", "openalex_id": "", "crossref_id": ""},
        "links": {"url": "", "pdf_url": "", "publisher_url": "", "repository_url": ""},
        "language": "en",
        "source": {"kind": "network_search", "provider": "crossref", "query": "", "retrieved_at": "", "raw_record_path": ""},
        "metadata_match": {"status": "matched", "source": "crossref", "confidence": 1.0, "matched_at": "", "warnings": []},
    })
    _write_json(folder / "stage_manifest.json", {
        "schema_version": "1.0",
        "paper_number": number,
        "paper_raw_id": number,
        "workflow_path": "network_metadata",
    })
    _write_json(_ledger_path(tmp_path), {
        "schema_version": "1.0",
        "max_number": "0000000000000001",
        "items": {},
    })

    ws = PaperRawAllocator(tmp_path / "paper_raw", ledger_path=_ledger_path(tmp_path), papers_dir=tmp_path / "papers").allocate_workspace()

    assert ws["paper_number"] == "0000000000000144"
    assert folder.exists()
    audit = _service(tmp_path).audit(detect_orphans=True)
    assert any(item["folder"].endswith(number) for item in audit["metadata_only_workspaces"])


def test_import_status_and_ledger_state_are_not_confused(tmp_path: Path):
    raw = tmp_path / "paper_raw"
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF import status")

    result = PaperRawAllocator(raw, ledger_path=_ledger_path(tmp_path), papers_dir=tmp_path / "papers").allocate_from_pdf(source)

    status = json.loads((Path(result["folder"]) / ".import_status.json").read_text(encoding="utf-8"))
    ledger = json.loads(_ledger_path(tmp_path).read_text(encoding="utf-8"))
    assert status["status"] == "ready_for_convert"
    assert ledger["items"][result["paper_number"]]["state"] == "metadata_staged"


def test_allocate_from_pdf_uses_same_stage_lock_and_no_recycle(tmp_path: Path):
    raw = tmp_path / "paper_raw"
    (raw / "0000000000000001").mkdir(parents=True)
    _write_json(_ledger_path(tmp_path), {
        "schema_version": "1.0",
        "max_number": "0000000000000000",
        "items": {},
    })
    source = tmp_path / "incoming.pdf"
    source.write_bytes(b"%PDF no recycle")

    result = PaperRawAllocator(raw, ledger_path=_ledger_path(tmp_path), papers_dir=tmp_path / "papers").allocate_from_pdf(source)

    assert result["paper_number"] == "0000000000000002"
    assert not (raw / ".allocate.lock").exists()
    ledger = json.loads(_ledger_path(tmp_path).read_text(encoding="utf-8"))
    assert ledger["max_number"] == "0000000000000002"
    assert ledger["items"]["0000000000000002"]["state"] == "metadata_staged"


def test_parallel_stage_network_metadata_records_allocates_unique_numbers(tmp_path: Path):
    paper_raw = tmp_path / "paper_raw"
    papers = tmp_path / "papers"
    ledger = _ledger_path(tmp_path)

    def records(offset: int) -> list[dict]:
        return [
            {"title": f"Paper {offset + i}", "year": 2024, "doi": f"10.1000/{offset + i}", "source": {"provider": "crossref"}}
            for i in range(30)
        ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(
            lambda rs: stage_network_metadata_records(
                rs,
                paper_raw_dir=paper_raw,
                papers_dir=papers,
                ledger_path=ledger,
                apply=True,
            ),
            [records(0), records(100)],
        ))

    staged_items = [item for report in reports for item in report["items"] if item["status"] == "staged"]
    numbers = [item["paper_number"] for item in staged_items]
    assert len(staged_items) == 60
    assert len(numbers) == len(set(numbers))
    assert all((paper_raw / number / f"{number}.metadata.json").exists() for number in numbers)
    assert all(report["failed_allocator"] == 0 for report in reports)
