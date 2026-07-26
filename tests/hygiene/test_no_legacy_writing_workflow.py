"""Regression guards for the single active writing workflow."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.check_directory_hygiene import check_directory_hygiene
from scripts.pack_repo import _should_pack
from src.writer.deep_reader import deep_read
from src.writer.job_manager import JobManager


pytestmark = pytest.mark.hygiene

ROOT = Path(__file__).resolve().parents[2]


FORBIDDEN_ACTIVE_TOKENS = [
    "data/llm_work",
    "write/<job",
    "write/{job",
    "global references.bib",
    "全局 references.bib",
    "从全局 references.bib 抽取",
    "catalog.metadata",
]

# Active security guards that legitimately forbid the legacy ``data/llm_work``
# path as an article source. These files are allowed to contain the literal
# token because they reject it at runtime (option A: keep the guard explicit).
LLM_WORK_GUARD_FILES = {
    "src/writer/deep_reader.py",
    "scripts/check_write_tex_project.py",
    "scripts/prepare_write_article_workdir.py",
    "src/utils/repository_hygiene.py",
}


def _active_files() -> list[Path]:
    roots = [
        ROOT / "src",
        ROOT / "scripts",
        ROOT / "skills",
        ROOT / "md",
        ROOT / "README.md",
        ROOT / "AGENTS.md",
        ROOT / "CLAUDE.md",
        ROOT / "write" / "README.md",
    ]
    excluded = {
        "scripts/pack_repo.py",
        "scripts/check_directory_hygiene.py",
    }
    out: list[Path] = []
    for root in roots:
        paths = [root] if root.is_file() else list(root.rglob("*"))
        for path in paths:
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel in excluded or "__pycache__" in path.parts:
                continue
            if path.suffix.lower() in {".pyc", ".png", ".jpg", ".jpeg", ".pdf", ".zip"}:
                continue
            out.append(path)
    return out


def test_active_files_do_not_recommend_old_writing_workflow():
    offenders: list[str] = []
    for path in _active_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        rel = path.relative_to(ROOT).as_posix()
        for token in FORBIDDEN_ACTIVE_TOKENS:
            # data/llm_work is allowed inside explicit security-guard files that
            # forbid it at runtime; everywhere else it indicates stale workflow.
            if token == "data/llm_work" and rel in LLM_WORK_GUARD_FILES:
                continue
            if token in text:
                offenders.append(f"{rel}: {token}")
    assert not offenders, "old writing workflow tokens found:\n" + "\n".join(offenders)


def test_job_manager_only_creates_write_jobs_root(tmp_path):
    jm = JobManager(write_dir=tmp_path / "write" / "jobs")
    info = jm.create(topic="new writer path")
    job_id = info["job_id"]

    assert (tmp_path / "write" / "jobs" / job_id).is_dir()
    assert not (tmp_path / "write" / job_id).exists()


def test_deep_read_requires_prepared_workset(tmp_path):
    jm = JobManager(write_dir=tmp_path / "write" / "jobs")
    info = jm.create(topic="missing workset")
    job_id = info["job_id"]
    (jm.job_dir(job_id) / "planning" / "selected_papers.json").write_text(json.dumps({
        "selected_papers": [{"paper_name": "2024_wang_test"}],
        "selection_status": "confirmed",
    }), encoding="utf-8")
    jm.set_step(job_id, "catalog_selection_confirmed", True)

    with pytest.raises(RuntimeError, match="create_write_job"):
        deep_read(job_id, jm=jm)


def test_api_exposes_prepare_workset_and_no_old_copy_endpoint(monkeypatch, tmp_path):
    import src.server as server

    client = TestClient(server.app)
    jm = JobManager(write_dir=tmp_path / "write" / "jobs")
    monkeypatch.setattr(server, "job_manager", jm)

    def fake_prepare(job_id, *, jm, catalog, overwrite=False, apply=True):
        job_dir = jm.job_dir(job_id)
        article = job_dir / "article" / "0000000000000001"
        article.mkdir(parents=True)
        manifest = {
            "job_id": job_id,
            "dry_run": False,
            "copied": [{"paper_name": "2024_wang_test", "paper_number": "0000000000000001", "work_dir": str(article)}],
            "skipped": [],
            "work_root": "write/jobs/<job_id>/article/",
        }
        (job_dir / "planning" / "workset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest

    monkeypatch.setattr(server, "prepare_workset", fake_prepare)

    response = client.post("/write/jobs", json={"topic": "api writer path"})
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    assert (tmp_path / "write" / "jobs" / job_id).is_dir()

    response = client.post(f"/write/jobs/{job_id}/prepare-workset", json={})
    assert response.status_code == 200
    assert (tmp_path / "write" / "jobs" / job_id / "article" / "0000000000000001").is_dir()
    assert not (tmp_path / "write" / job_id).exists()

    old = client.post("/papers/by-number/0000000000000001/copy-to-llm-work", json={"session_id": "demo"})
    assert old.status_code in (404, 405)


def test_pack_and_hygiene_guard_runtime_artifacts(tmp_path):
    assert _should_pack("write/jobs/demo/tex/main.tex") is False
    assert _should_pack("data/llm_work/demo/000001/full.md") is False
    assert _should_pack("write/001_old/tex/main.tex") is False

    assert _should_pack("data/catalog/all/0000000000000001") is False
