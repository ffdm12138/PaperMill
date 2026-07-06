"""prepare_workset + deep_read 路径对齐测试：active 主路径走 write/jobs/<job_id>/article/，
不再依赖 data/llm_work/。不访问网络，不依赖真实 data/papers。"""
import json
from pathlib import Path

import pytest

from src.writer.job_manager import JobManager
from src.writer.catalog_matcher import confirm_selected_papers
from src.writer.deep_reader import deep_read, prepare_workset


class FakeLibrary:
    """PaperLibrary 替身（workset 路径下 deep_read 不应调用它）。"""

    def exists(self, pid):
        raise AssertionError("workset 路径不应回退到 PaperLibrary")

    def list_images(self, pid):
        raise AssertionError("workset 路径不应回退到 PaperLibrary")

    def read_multiple(self, paper_ids, max_chars_each=0):
        raise AssertionError("workset 路径不应回退到 PaperLibrary")


class FakeCatalog:
    """content-only catalog 替身：只含 paper_id / paper_number。"""

    def __init__(self, entries):
        self._entries = entries
        self._by_id = {e["paper_id"]: e for e in entries}

    def list_papers(self):
        return list(self._entries)

    def get(self, pid_or_number):
        return self._by_id.get(pid_or_number)


def _make_formal_paper(papers_dir: Path, paper_id: str, number: str):
    folder = papers_dir / paper_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{paper_id}.md").write_text(f"# {paper_id} full text\n\n正文内容。", encoding="utf-8")
    (folder / "images").mkdir(exist_ok=True)
    return folder


def _make_job(tmp_path: Path):
    jm = JobManager(write_dir=tmp_path / "write" / "jobs")
    info = jm.create(topic="路径对齐测试")
    return jm, info["job_id"]


def test_prepare_workset_copies_into_write_jobs_article(tmp_path):
    papers_dir = tmp_path / "data" / "papers"
    paper_id = "2024_wang_测试论文"
    number = "0000000000000001"
    _make_formal_paper(papers_dir, paper_id, number)

    jm, job_id = _make_job(tmp_path)
    catalog = FakeCatalog([{"paper_id": paper_id, "paper_number": number}])
    confirm_selected_papers(job_id, [{"paper_id": paper_id}], jm=jm, catalog=catalog)

    manifest = prepare_workset(job_id, jm=jm, papers_dir=papers_dir, catalog=catalog)

    jdir = jm.job_dir(job_id)
    article_target = jdir / "article" / number
    # 复制到 write/jobs/<job_id>/article/<paper_number>/，而非 data/llm_work
    assert article_target.exists()
    assert (article_target / f"{paper_id}.md").exists()
    assert "data/llm_work" not in manifest["work_root"]
    assert manifest["work_root"] == "write/jobs/<job_id>/article/"
    assert manifest["copied"][0]["work_dir"].replace("\\", "/").endswith(f"article/{number}")
    # manifest 落在 job 目录
    assert (jdir / "planning" / "workset_manifest.json").exists()


def test_prepare_workset_rejects_unconfirmed(tmp_path):
    jm, job_id = _make_job(tmp_path)
    with pytest.raises(RuntimeError, match="not confirmed"):
        prepare_workset(job_id, jm=jm, papers_dir=tmp_path / "papers",
                        catalog=FakeCatalog([]))


def test_prepare_workset_forbids_llm_work_source(tmp_path):
    """article 来源必须是正式 papers 目录，禁止 data/llm_work。"""
    paper_id = "2024_wang_测试论文"
    number = "0000000000000001"
    jm, job_id = _make_job(tmp_path)
    catalog = FakeCatalog([{"paper_id": paper_id, "paper_number": number}])
    confirm_selected_papers(job_id, [{"paper_id": paper_id}], jm=jm, catalog=catalog)
    # papers_dir 指向不存在的 llm_work 路径 → 跳过（formal_folder_missing_or_forbidden）
    manifest = prepare_workset(job_id, jm=jm,
                               papers_dir=tmp_path / "data" / "llm_work",
                               catalog=catalog)
    assert manifest["copied"] == []
    assert any("forbidden" in s["reason"] or "missing" in s["reason"]
               for s in manifest["skipped"])


def test_deep_read_reads_from_write_jobs_article_not_llm_work(tmp_path):
    papers_dir = tmp_path / "data" / "papers"
    paper_id = "2024_wang_测试论文"
    number = "0000000000000001"
    _make_formal_paper(papers_dir, paper_id, number)

    jm, job_id = _make_job(tmp_path)
    catalog = FakeCatalog([{"paper_id": paper_id, "paper_number": number}])
    confirm_selected_papers(job_id, [{"paper_id": paper_id}], jm=jm, catalog=catalog)
    prepare_workset(job_id, jm=jm, papers_dir=papers_dir, catalog=catalog)

    result = deep_read(job_id, jm=jm, catalog=catalog)

    # source 标识新路径，不含 data/llm_work
    assert "write/jobs" in result["source"]
    assert "article" in result["source"]
    assert "llm_work" not in result["source"]
    # 笔记模板已生成
    jdir = jm.job_dir(job_id)
    assert (jdir / "reading" / "paper_notes" / f"{paper_id}.md").exists()
    assert (jdir / "reading" / "evidence_table.md").exists()
