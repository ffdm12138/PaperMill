import json
import runpy
import sys
from pathlib import Path

from scripts.rollback_formal_papers_to_paper_raw import rollback_formal_papers
from src.services.asset_manifest import read_asset_manifest
from src.services.v2_library import PaperNumberLedger
from tests.helpers.paper_raw_factory import commit_for_test, formalize_for_test, make_staged_source


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "rollback_formal_papers_to_paper_raw.py"


def _commit_formal(tmp_path: Path, *, source_id: str = "0000000000000001", title_zh: str = "可信论文", **kw) -> tuple[Path, str]:
    source = make_staged_source(tmp_path, source_id, title_zh=title_zh, **kw)
    formalized = formalize_for_test(
        tmp_path,
        source,
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
    )
    assert formalized["success"], formalized
    result = commit_for_test(
        tmp_path,
        Path(formalized["folder"]),
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
    )
    assert result["status"] == "imported", result
    return tmp_path / "papers" / result["paper_id"], result["paper_number"]


def _rollback(tmp_path: Path, **kwargs):
    return rollback_formal_papers(
        papers_dir=tmp_path / "papers",
        paper_raw_dir=tmp_path / "paper_raw",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        archive_dir=tmp_path / "transactions" / "rollback",
        all_papers=True,
        apply=True,
        **kwargs,
    )


def _run_cli(argv: list[str]) -> int:
    saved = sys.argv
    sys.argv = argv
    try:
        runpy.run_path(str(_SCRIPT), run_name="__main__")
        return 0
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = saved


def test_rollback_preserves_ledger_and_source_records_and_deletes_catalog(tmp_path):
    paper_dir, number = _commit_formal(tmp_path)
    pid = paper_dir.name
    (paper_dir / "source_records").mkdir()
    (paper_dir / "source_records" / "crossref.json").write_text('{"ok": true}', encoding="utf-8")
    before = json.loads((tmp_path / "catalog" / "paper_number_ledger.json").read_text(encoding="utf-8"))

    report = _rollback(tmp_path)

    raw = tmp_path / "paper_raw" / number
    assert report["summary"]["rolled_back"] == 1
    assert not paper_dir.exists()
    assert (tmp_path / "transactions" / "rollback" / "papers_backup" / pid).exists()
    assert (raw / f"{number}.metadata.json").exists()
    assert (raw / f"{number}.md").exists()
    assert (raw / f"{number}.pdf").exists()
    assert (raw / "images").is_dir()
    assert (raw / "source_records" / "crossref.json").exists()
    assert not list(raw.glob("*.catalog.json"))

    ledger = json.loads((tmp_path / "catalog" / "paper_number_ledger.json").read_text(encoding="utf-8"))
    assert ledger["max_number"] == before["max_number"]
    item = ledger["items"][number]
    assert item["state"] == "reserved"
    assert item["folder_name"] == number
    assert item["activated_at"]
    assert item["rolled_back_at"]

    manifest = read_asset_manifest(raw, number)
    assert manifest["paper_id"] == ""
    assert manifest["stage"] == "paper_raw"
    assert manifest["files"]["pdf"]["path"] == f"{number}.pdf"

    all_catalog = json.loads((tmp_path / "catalog" / "all.catalog.json").read_text(encoding="utf-8"))
    paper_index = json.loads((tmp_path / "catalog" / "paper_index.json").read_text(encoding="utf-8"))
    assert all_catalog["papers"] == []
    assert paper_index["papers"] == []


def test_rollback_keep_catalog_is_explicit_debug_mode(tmp_path):
    _, number = _commit_formal(tmp_path)

    _rollback(tmp_path, keep_catalog=True)

    assert (tmp_path / "paper_raw" / number / f"{number}.catalog.json").exists()


def test_rollback_target_exists_blocks_without_archiving_formal(tmp_path):
    paper_dir, number = _commit_formal(tmp_path)
    (tmp_path / "paper_raw" / number).mkdir(parents=True)

    report = _rollback(tmp_path)

    assert report["summary"]["blocking_errors"] > 0
    assert paper_dir.exists()
    assert not (tmp_path / "transactions" / "rollback" / "papers_backup" / paper_dir.name).exists()


def test_rollback_postcheck_failure_does_not_remove_formal(tmp_path, monkeypatch):
    import scripts.rollback_formal_papers_to_paper_raw as rollback_cli

    paper_dir, number = _commit_formal(tmp_path)

    def _fail_postcheck(folder, paper_number, *, keep_catalog=False):
        return ["forced staging postcheck failure"]

    monkeypatch.setattr(rollback_cli, "_postcheck_staging", _fail_postcheck)
    report = _rollback(tmp_path)

    assert report["items"][0]["status"] == "failed"
    assert report["summary"]["failed"] == 1
    assert report["summary"]["blocking_errors"] == 1
    assert paper_dir.exists()
    assert not (tmp_path / "paper_raw" / number).exists()


def test_rollback_cli_failed_item_exits_nonzero(tmp_path, monkeypatch):
    import src.services.v2_library as v2_library

    paper_dir, number = _commit_formal(tmp_path)

    def _skip_conversion_manifest(folder, file_prefix):
        return {"status": "converted"}

    monkeypatch.setattr(v2_library, "write_conversion_manifest_for_existing_assets", _skip_conversion_manifest)

    rc = _run_cli([
        "rollback_formal_papers_to_paper_raw.py",
        "--all",
        "--papers-dir", str(tmp_path / "papers"),
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--ledger-path", str(tmp_path / "catalog" / "paper_number_ledger.json"),
        "--all-catalog-path", str(tmp_path / "catalog" / "all.catalog.json"),
        "--archive-dir", str(tmp_path / "transactions" / "rollback"),
        "--apply",
    ])

    assert rc == 1
    assert paper_dir.exists()
    assert not (tmp_path / "paper_raw" / number).exists()


def test_single_paper_rollback_skips_index_write_when_remaining_catalog_invalid(tmp_path):
    paper_a, number_a = _commit_formal(
        tmp_path,
        source_id="0000000000000001",
        title_zh="甲论文",
        title_original="Paper A",
        doi="10.1/a",
        family="Wang",
    )
    paper_b, _ = _commit_formal(
        tmp_path,
        source_id="0000000000000002",
        title_zh="乙论文",
        title_original="Paper B",
        doi="10.1/b",
        family="Li",
        pdf_bytes=b"%PDF-B",
    )
    all_catalog = tmp_path / "catalog" / "all.catalog.json"
    before = all_catalog.read_text(encoding="utf-8")
    (paper_b / f"{paper_b.name}.catalog.json").write_text("{", encoding="utf-8")

    report = rollback_formal_papers(
        papers_dir=tmp_path / "papers",
        paper_raw_dir=tmp_path / "paper_raw",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=all_catalog,
        archive_dir=tmp_path / "transactions" / "rollback",
        paper_id=paper_a.name,
        apply=True,
    )

    assert report["summary"]["rolled_back"] == 1
    assert report["summary"]["blocking_errors"] > 0
    assert report["index_rebuild"]["status"] == "failed_skipped_write"
    assert all_catalog.read_text(encoding="utf-8") == before
    assert (tmp_path / "paper_raw" / number_a).exists()


def test_rollback_strict_only_blocks_inline_raw_record(tmp_path):
    paper_dir, _ = _commit_formal(tmp_path)
    meta_path = paper_dir / f"{paper_dir.name}.metadata.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    metadata["source"]["raw_record"] = {"legacy": True}
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")

    report = _rollback(tmp_path)

    assert report["summary"]["blocking_errors"] > 0
    assert any("raw_record" in err for err in report["items"][0]["errors"])
    assert paper_dir.exists()


def test_rollback_multi_paper_failed_item_skips_index_rebuild(tmp_path, monkeypatch):
    """Two formal papers, one fails postcheck → index_rebuild skipped, CLI exit ≠ 0."""
    import scripts.rollback_formal_papers_to_paper_raw as rollback_module

    # Commit two papers.  Use source_ids that sort so both papers are processed
    # and we can predict which one the monkeypatch will trip.
    paper_a, number_a = _commit_formal(
        tmp_path,
        source_id="0000000000000001",
        title_zh="甲论文",
        title_original="Paper A",
        doi="10.1/a",
        family="Wang",
    )
    paper_b, number_b = _commit_formal(
        tmp_path,
        source_id="0000000000000002",
        title_zh="乙论文",
        title_original="Paper B",
        doi="10.1/b",
        family="Li",
        pdf_bytes=b"%PDF-B",
    )
    all_catalog_path = tmp_path / "catalog" / "all.catalog.json"
    before_catalog = all_catalog_path.read_text(encoding="utf-8")
    before_index = (tmp_path / "catalog" / "paper_index.json").read_text(encoding="utf-8")

    # Force paper_a's staging postcheck to fail (by paper_number, not call count,
    # since sort order determines which paper is processed first).
    _original_postcheck = rollback_module._postcheck_staging

    def _fail_for_number(folder, paper_number, *, keep_catalog=False):
        if paper_number == number_a:
            return ["forced staging postcheck failure"]
        return _original_postcheck(folder, paper_number, keep_catalog=keep_catalog)

    monkeypatch.setattr(rollback_module, "_postcheck_staging", _fail_for_number)

    report = rollback_formal_papers(
        papers_dir=tmp_path / "papers",
        paper_raw_dir=tmp_path / "paper_raw",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=all_catalog_path,
        archive_dir=tmp_path / "transactions" / "rollback",
        all_papers=True,
        apply=True,
    )

    # One paper should rollback, one should fail.
    assert report["summary"]["rolled_back"] == 1
    assert report["summary"]["failed"] == 1
    # Index rebuild must be skipped.
    assert report["index_rebuild"]["status"] == "skipped_due_to_failed_items"
    # Catalogs/indexes must be untouched.
    assert all_catalog_path.read_text(encoding="utf-8") == before_catalog
    assert (tmp_path / "catalog" / "paper_index.json").read_text(encoding="utf-8") == before_index
    # Paper A failed → still in formal, not in raw. Paper B rolled back.
    assert paper_a.exists()
    assert not paper_b.exists()
    assert not (tmp_path / "paper_raw" / number_a).exists()
    assert (tmp_path / "paper_raw" / number_b).exists()


def test_ledger_rollback_active_to_reserved_rejects_non_active(tmp_path):
    folder = tmp_path / "paper_raw" / "0000000000000001"
    folder.mkdir(parents=True)
    ledger = PaperNumberLedger(tmp_path / "catalog" / "paper_number_ledger.json")
    ledger.reserve_specific_for_paper_raw("0000000000000001", folder)

    try:
        ledger.rollback_active_to_reserved("0000000000000001", folder)
    except ValueError as exc:
        assert "state reserved" in str(exc)
    else:
        raise AssertionError("reserved number should not rollback as active")
