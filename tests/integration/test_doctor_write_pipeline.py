import argparse
import json
from pathlib import Path

from scripts import doctor_write_pipeline as doctor


def _args(tmp_path: Path, **kwargs) -> argparse.Namespace:
    defaults = {
        "job_id": None,
        "catalog_root": tmp_path / "data" / "catalog",
        "write_dir": tmp_path / "write" / "jobs",
        "repo_root": Path(__file__).resolve().parent.parent.parent,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_doctor_without_catalog_is_source_valid_but_runtime_not_ready(tmp_path):
    report = doctor.doctor_write_pipeline(_args(tmp_path))

    assert report["valid"] is True
    assert report["source_valid"] is True
    assert report["runtime_ready"] is False
    assert report["environment"]["catalog_folders"]["ready"] is False
    assert report["errors"] == []
    assert any("reconcile_catalog_folders.py --apply" in warning for warning in report["warnings"])


def test_doctor_identifies_missing_job(tmp_path):
    catalog = tmp_path / "data" / "catalog" / "all.catalog.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({"papers": []}), encoding="utf-8")

    report = doctor.doctor_write_pipeline(_args(tmp_path, job_id="missing_job"))

    assert report["job"]["status"] == "missing"
    assert report["valid"] is False
    assert report["source_valid"] is False
    assert any("write job not found" in error for error in report["errors"])


def test_doctor_identifies_prepared_job_without_tex(tmp_path):
    catalog = tmp_path / "data" / "catalog" / "all.catalog.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({"papers": []}), encoding="utf-8")
    job_dir = tmp_path / "write" / "jobs" / "prepared_job"
    (job_dir / "article" / "0000000000000001").mkdir(parents=True)
    (job_dir / "reports").mkdir()

    report = doctor.doctor_write_pipeline(_args(tmp_path, job_id="prepared_job"))

    assert report["job"]["status"] == "prepared"
    assert report["job"]["article_exists"] is True
    assert report["job"]["tex_exists"] is False


def test_write_jobs_tracking_check_allows_only_gitkeep(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "_git_ls_files", lambda repo_root, path: ["write/jobs/.gitkeep"])

    result = doctor._check_write_jobs_tracking(tmp_path)

    assert result["ok"] is True
    assert result["unexpected"] == []


def test_write_jobs_tracking_check_flags_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(
        doctor,
        "_git_ls_files",
        lambda repo_root, path: ["write/jobs/.gitkeep", "write/jobs/job_a/tex/main.tex"],
    )

    result = doctor._check_write_jobs_tracking(tmp_path)

    assert result["ok"] is False
    assert result["unexpected"] == ["write/jobs/job_a/tex/main.tex"]
