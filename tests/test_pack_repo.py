"""pack_repo 卫生测试：硬排除 write/jobs、data/llm_work、legacy write/<job> 运行产物。

只测 _should_pack 的纯函数行为，不访问网络，不依赖真实 data/papers。
"""
from pathlib import Path

from scripts.pack_repo import _should_pack


def test_should_pack_excludes_write_jobs_runtime():
    assert _should_pack("write/jobs/demo/tex/main.tex") is False
    assert _should_pack("write/jobs/demo/references.bib") is False
    assert _should_pack("write/jobs/demo/article/0000000000000001/full.md") is False


def test_should_pack_excludes_legacy_write_job_dir():
    assert _should_pack("write/001_legacy/tex/main.tex") is False
    assert _should_pack("write/job_demo/tex/main.tex") is False


def test_should_pack_excludes_data_llm_work():
    assert _should_pack("data/llm_work/demo/000001/full.md") is False
    assert _should_pack("data/llm_work/demo/000001/paper.md") is False


def test_should_pack_keeps_write_docs_and_gitkeep():
    assert _should_pack("write/README.md") is True
    assert _should_pack("write/.gitkeep") is True
    assert _should_pack("write/jobs/.gitkeep") is True


def test_should_pack_keeps_source_docs():
    assert _should_pack("docs/PROJECT_CONTRACT.md") is True
    assert _should_pack("README.md") is True
    assert _should_pack("src/writer/job_manager.py") is True


def test_should_pack_excludes_real_data_dirs():
    assert _should_pack("data/papers/2024_x/2024_x.pdf") is False
    assert _should_pack("data/paper_raw/000001/000001.md") is False
    assert _should_pack("data/raw/some.pdf") is False
    assert _should_pack("data/import_work/x/file.pdf") is False


def test_should_pack_excludes_reports_runtime_json():
    assert _should_pack("reports/doctor_ingest_pipeline_report.json") is False
    assert _should_pack("reports/commit_report.json") is False
    assert _should_pack("reports/stage_raw_move.json") is False
    assert _should_pack("reports/dup_dryrun.err") is False
    assert _should_pack("reports/real_ingest_acceptance.md") is True
    assert _should_pack("reports/code_audit_2026-06-30.md") is True
