import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.check_directory_hygiene import check_directory_hygiene


def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_directory_hygiene_reports_warnings_without_deleting_files(tmp_path):
    papers_dir = tmp_path / "data" / "papers"
    paper_raw_dir = tmp_path / "data" / "paper_raw"
    write_jobs_dir = tmp_path / "write" / "jobs"
    all_catalog = tmp_path / "data" / "catalog" / "all.catalog.json"

    paper = papers_dir / "2024_author_missing_doi"
    paper.mkdir(parents=True)
    _write_json(paper / "2024_author_missing_doi.metadata.json", {"identifiers": {"doi": ""}})
    _write_json(all_catalog, {
        "papers": [
            {"paper_number": "0000000000000001", "paper_id": "2024_author_missing_doi"},
            {"paper_number": "0000000000000001", "paper_id": "missing_folder"},
        ]
    })
    _write_json(paper_raw_dir / "0000000000000001" / "0000000000000001.metadata.json", {"identifiers": {"doi": "10.1/duplicate"}})

    job = write_jobs_dir / "job"
    (job / "tex").mkdir(parents=True)
    (job / "tex" / "main.tex").write_text("% data/paper_raw/0000000000000001\n", encoding="utf-8")
    _write_json(job / "selected_catalog.json", {
        "papers": [{"source_dir": "data/paper_raw/0000000000000001", "paper_number": "0000000000000001"}]
    })

    report = check_directory_hygiene(
        project_root=tmp_path,
        all_catalog_path=all_catalog,
        papers_dir=papers_dir,
        paper_raw_dir=paper_raw_dir,
        write_jobs_dir=write_jobs_dir,
    )

    assert report["valid"] is True
    assert report["warning_count"] >= 4
    assert (job / "tex" / "main.tex").exists()
    assert any("duplicate paper_number" in warning for warning in report["warnings"])
    assert any("metadata.identifiers.doi missing" in warning for warning in report["warnings"])
    assert any("missing formal paper folder" in warning for warning in report["warnings"])
    assert any("data/paper_raw" in warning for warning in report["warnings"])


def _hygiene_with_paper_raw(tmp_path, paper_raw_meta: dict, *, md: bool = False,
                            candidates: bool = False, resolve_report: bool = False,
                            import_status: str | None = None):
    paper_raw_dir = tmp_path / "data" / "paper_raw"
    all_catalog = tmp_path / "data" / "catalog" / "all.catalog.json"
    folder = paper_raw_dir / "0000000000000001"
    folder.mkdir(parents=True)
    _write_json(folder / "0000000000000001.metadata.json", paper_raw_meta)
    if md:
        (folder / "0000000000000001.md").write_text("# title", encoding="utf-8")
    if candidates:
        _write_json(folder / "0000000000000001.metadata.candidates.json", {"candidates": []})
    if resolve_report:
        _write_json(folder / "0000000000000001.metadata.resolve_report.json", {"decision": "manual_review"})
    if import_status is not None:
        _write_json(folder / ".import_status.json", {"status": import_status})
    _write_json(all_catalog, {"papers": []})
    return check_directory_hygiene(
        project_root=tmp_path,
        all_catalog_path=all_catalog,
        papers_dir=tmp_path / "data" / "papers",
        paper_raw_dir=paper_raw_dir,
        write_jobs_dir=tmp_path / "write" / "jobs",
    )


def test_hygiene_warns_md_present_but_unmatched(tmp_path):
    report = _hygiene_with_paper_raw(
        tmp_path, {"metadata_match": {"status": "unmatched"}}, md=True,
    )
    assert report["valid"] is True
    assert any("has markdown but metadata_match.status is unmatched" in w for w in report["warnings"])
    # no files deleted
    assert ((tmp_path / "data" / "paper_raw" / "0000000000000001" / "0000000000000001.md")).exists()


def test_hygiene_warns_unresolved_candidates(tmp_path):
    report = _hygiene_with_paper_raw(
        tmp_path, {"metadata_match": {"status": "unmatched"}}, candidates=True,
    )
    assert report["valid"] is True
    assert any("unresolved metadata candidates" in w for w in report["warnings"])


def test_hygiene_warns_stuck_import_status(tmp_path):
    report = _hygiene_with_paper_raw(
        tmp_path, {"metadata_match": {"status": "unmatched"}},
        import_status="metadata_candidates_found",
    )
    assert report["valid"] is True
    assert any("stuck at metadata_candidates_found" in w for w in report["warnings"])


def test_hygiene_warns_stale_write_job_dir(tmp_path):
    """stale write runtime artifacts outside write/jobs are warned, not deleted."""
    write_root = tmp_path / "write"
    write_jobs_dir = write_root / "jobs"
    legacy_job = write_root / "001_legacy"
    (legacy_job / "tex").mkdir(parents=True)
    (legacy_job / "tex" / "main.tex").write_text("% legacy job", encoding="utf-8")
    all_catalog = tmp_path / "data" / "catalog" / "all.catalog.json"
    _write_json(all_catalog, {"papers": []})

    report = check_directory_hygiene(
        project_root=tmp_path,
        all_catalog_path=all_catalog,
        papers_dir=tmp_path / "data" / "papers",
        paper_raw_dir=tmp_path / "data" / "paper_raw",
        write_jobs_dir=write_jobs_dir,
        write_root=write_root,
    )

    assert report["valid"] is True
    assert any("stale write runtime artifact present" in w for w in report["warnings"])
    # 涓嶅垹闄?    assert (legacy_job / "tex" / "main.tex").exists()

