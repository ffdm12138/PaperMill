"""Track A 多阶段 writer 的 BibTeX / cite-key 必须完全 job-local。

验证：
  - export_job_bib 从 write/jobs/<job_id>/article/<paper_number>/*.metadata.json 生成
    references.bib，不再读全局 all.catalog / data/papers；
  - deep_read 的 \\cite{} key、copy_figures 的 bib_key、references.bib 的 entry key
    三者一致，且都等于 job-local metadata.citation_key；
  - 缺 article metadata 时 export_job_bib 显式报错，而不是静默生成 0 条；
  - write_review confirm-papers 的下一步提示包含 prepare-workset；
  - selected_catalog.json 为 content-only，不含 metadata 字段。
"""
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.writer.job_manager import JobManager
from src.writer.catalog_matcher import confirm_selected_papers
from src.writer.deep_reader import deep_read, prepare_workset
from src.writer.figure_manager import copy_figures
from src.writer.bib_manager import export_job_bib
from src.writer.tex_project import build_tex


class FakeCatalog:
    """content-only catalog 替身：只含 paper_id / paper_number。"""

    def __init__(self, entries):
        self._entries = entries
        self._by_id = {e["paper_id"]: e for e in entries}

    def list_papers(self):
        return list(self._entries)

    def get(self, pid_or_number):
        return self._by_id.get(pid_or_number)


def _make_formal_paper(papers_dir: Path, paper_id: str, number: str,
                       citation_key: str = "wang2024test",
                       doi: str = "10.5555/test.1",
                       with_metadata: bool = True) -> Path:
    folder = papers_dir / paper_id
    (folder / "images").mkdir(parents=True, exist_ok=True)
    (folder / f"{paper_id}.md").write_text(f"# {paper_id}\n\n正文内容。", encoding="utf-8")
    (folder / "images" / "fig1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    if with_metadata:
        meta = {
            "citation_key": citation_key,
            "title": {"original": "Test Paper"},
            "authors": [{"full_name": "A. Wang", "family": "Wang", "given": "A."}],
            "year": 2024,
            "container": {"journal": "Test Journal"},
            "identifiers": {"doi": doi},
        }
        (folder / f"{paper_id}.metadata.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return folder


def _make_job(tmp_path: Path):
    jm = JobManager(write_dir=tmp_path / "write" / "jobs")
    info = jm.create(topic="job-local bib 测试")
    return jm, info["job_id"]


def test_export_job_bib_uses_job_local_metadata_not_global(tmp_path):
    """article metadata 存在时 export_job_bib 生成 1 条 BibTeX，key=citation_key。"""
    papers_dir = tmp_path / "data" / "papers"
    paper_id = "2024_wang_testpaper"
    number = "0000000000000001"
    _make_formal_paper(papers_dir, paper_id, number, citation_key="wang2024test")

    jm, job_id = _make_job(tmp_path)
    catalog = FakeCatalog([{"paper_id": paper_id, "paper_number": number}])
    confirm_selected_papers(job_id, [{"paper_id": paper_id}], jm=jm, catalog=catalog)
    prepare_workset(job_id, jm=jm, papers_dir=papers_dir, catalog=catalog)

    # export_job_bib 只读 job-local article metadata，不依赖全局 catalog。
    info = export_job_bib(job_id, jm=jm)
    assert info["count"] == 1

    jdir = jm.job_dir(job_id)
    bib = (jdir / "tex" / "references.bib").read_text(encoding="utf-8")
    assert "@article{wang2024test," in bib
    assert "10.5555/test.1" in bib


def test_cite_keys_consistent_across_deep_read_figures_and_bib(tmp_path):
    """deep_read / copy_figures / references.bib 的 cite key 必须一致且来自 job-local metadata。"""
    papers_dir = tmp_path / "data" / "papers"
    paper_id = "2024_wang_consistent"
    number = "0000000000000002"
    _make_formal_paper(papers_dir, paper_id, number, citation_key="wang2024consistent")

    jm, job_id = _make_job(tmp_path)
    catalog = FakeCatalog([{"paper_id": paper_id, "paper_number": number}])
    confirm_selected_papers(job_id, [{"paper_id": paper_id}], jm=jm, catalog=catalog)
    prepare_workset(job_id, jm=jm, papers_dir=papers_dir, catalog=catalog)
    jdir = jm.job_dir(job_id)

    # deep_read 笔记模板的 \cite{} key
    deep_read(job_id, jm=jm, catalog=catalog)
    note = (jdir / "reading" / "paper_notes" / f"{paper_id}.md").read_text(encoding="utf-8")
    m = re.search(r"\\cite\{([^}]*)\}", note)
    assert m is not None
    note_key = m.group(1)
    assert note_key == "wang2024consistent"

    # copy_figures 的 source record bib_key
    copy_figures(job_id, figures=[{"paper_id": paper_id, "image": "fig1.png"}], jm=jm)
    readme = (jdir / "figures" / paper_id / "README.md").read_text(encoding="utf-8")
    assert f"bib_key: {note_key}" in readme

    # references.bib 的 entry key
    export_job_bib(job_id, jm=jm)
    bib = (jdir / "tex" / "references.bib").read_text(encoding="utf-8")
    assert f"@article{{{note_key}," in bib


def test_export_job_bib_raises_when_article_metadata_missing(tmp_path):
    """缺 article metadata 时必须显式报错，而不是静默生成 0 条 references.bib。"""
    papers_dir = tmp_path / "data" / "papers"
    paper_id = "2024_wang_nometa"
    number = "0000000000000003"
    _make_formal_paper(papers_dir, paper_id, number, with_metadata=False)

    jm, job_id = _make_job(tmp_path)
    catalog = FakeCatalog([{"paper_id": paper_id, "paper_number": number}])
    confirm_selected_papers(job_id, [{"paper_id": paper_id}], jm=jm, catalog=catalog)
    prepare_workset(job_id, jm=jm, papers_dir=papers_dir, catalog=catalog)

    with pytest.raises(RuntimeError, match="metadata missing"):
        export_job_bib(job_id, jm=jm)


def test_confirm_papers_next_step_mentions_prepare_workset(tmp_path, monkeypatch):
    """confirm-papers 后的下一步提示必须包含 prepare-workset。"""
    jm = JobManager(write_dir=tmp_path / "write" / "jobs")
    info = jm.create(topic="cli next step")
    job_id = info["job_id"]
    paper_id = "2024_wang_clinext"
    number = "0000000000000004"
    fake = FakeCatalog([{"paper_id": paper_id, "paper_number": number}])

    import scripts.write_review as wr
    import src.writer.catalog_matcher as cm
    monkeypatch.setattr(wr, "Catalog", lambda: fake)
    monkeypatch.setattr(cm, "Catalog", lambda: fake)
    # confirm_selected_papers 默认 JobManager() 指向真实 write/jobs；改成 tmp jm。
    monkeypatch.setattr(cm, "JobManager", lambda: jm)

    args = SimpleNamespace(job=job_id, papers=[paper_id], paper_numbers=None)
    messages = []
    sink_id = wr.logger.add(messages.append, level="INFO")
    try:
        wr.cmd_confirm_papers(args)
    finally:
        wr.logger.remove(sink_id)

    assert any("prepare-workset" in m for m in messages)


def test_build_tex_template_only_tolerates_missing_workset(tmp_path):
    """template-only 模式在未运行 prepare-workset 时仍可生成空模板（escape hatch）。"""
    jm = JobManager(write_dir=tmp_path / "write" / "jobs")
    info = jm.create(topic="template only")
    job_id = info["job_id"]

    # 没有 confirm / prepare-workset → workset_manifest.json 缺失；
    # template_only=True 必须不报错并写出占位 references.bib。
    result = build_tex(job_id, template_only=True, jm=jm)
    jdir = jm.job_dir(job_id)
    assert (jdir / "tex" / "main.tex").exists()
    bib = (jdir / "tex" / "references.bib").read_text(encoding="utf-8")
    assert "template-only mode" in bib
    assert result["bib_count"] == 0


def test_selected_catalog_is_content_only_metadata_in_article(tmp_path):
    """Track B selected_catalog.json 不含 metadata；DOI 在 article/<n>/*.metadata.json。"""
    import argparse
    from scripts.prepare_write_article_workdir import prepare_workdir
    from src.services.v2_library import empty_catalog, empty_metadata
    from tests.helpers.paper_raw_factory import fill_valid_catalog_v31

    pid = "2024_author_selcat"
    number = "0000000000000005"
    papers_dir = tmp_path / "data" / "papers"
    folder = papers_dir / pid
    (folder / "images").mkdir(parents=True)
    catalog = fill_valid_catalog_v31(
        empty_catalog(),
        paper_number=number,
        title_zh="精选目录论文",
        title_original="Selected Catalog Paper",
        domain="writer_test",
    )
    catalog["library_locator"]["paper_id"] = pid
    meta = empty_metadata(pid)
    meta["title"]["original"] = "Selected Catalog Paper"
    meta["year"] = 2024
    meta["authors"] = [{"full_name": "A. Author", "family": "Author", "given": "A.", "orcid": "", "affiliation": ""}]
    meta["identifiers"]["doi"] = "10.5555/selcat.1"
    (folder / f"{pid}.catalog.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    (folder / f"{pid}.metadata.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (folder / f"{pid}.md").write_text("# x\n", encoding="utf-8")
    (folder / f"{pid}.pdf").write_bytes(b"%PDF")

    all_catalog = tmp_path / "data" / "catalog" / "all.catalog.json"
    all_catalog.parent.mkdir(parents=True, exist_ok=True)
    all_catalog.write_text(json.dumps({
        "schema_version": "3.1", "updated_at": "",
        "papers": [{
            "paper_number": number, "paper_id": pid,
            "paper_dir": str(folder),
            "asset_refs": {"metadata": "", "catalog": "", "markdown": "", "pdf": "", "images_dir": ""},
            "content_identity": catalog["content_identity"],
            "terminology": catalog["terminology"],
            "classification": catalog["classification"],
            "screening": catalog["screening"],
            "research_card": catalog["research_card"],
            "writing_value": catalog["writing_value"],
            "evidence_profile": catalog["evidence_profile"],
            "figure_inventory": catalog["figure_inventory"],
            "quality_control": catalog["quality_control"],
            "provenance": catalog["provenance"],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    write_dir = tmp_path / "write" / "jobs"

    args = argparse.Namespace(
        job_id="selcat_job", paper_numbers=[number],
        primary_domain=None, topic=None, read_decision=None,
        min_relevance_score=None, limit=None,
        apply=True, dry_run=False, overwrite=False,
        all_catalog=all_catalog, papers_dir=papers_dir, write_dir=write_dir,
    )
    prepare_workdir(args)

    job_dir = write_dir / "selcat_job"
    selected = json.loads((job_dir / "selected_catalog.json").read_text(encoding="utf-8"))
    p0 = selected["papers"][0]
    # selected_catalog is strictly content-only: no metadata, no path fields.
    for forbidden in ("metadata", "formal_paper_dir", "article_dir",
                      "source_dir", "folder_path", "main_md",
                      "metadata_file", "catalog_file"):
        assert forbidden not in p0, f"selected_catalog paper carries {forbidden}"
    assert "classification" in p0 and "screening" in p0
    article_meta = json.loads(
        (job_dir / "article" / number / f"{pid}.metadata.json").read_text(encoding="utf-8"))
    assert article_meta["identifiers"]["doi"] == "10.5555/selcat.1"
    # Path tracking lives in the report, not selected_catalog.
    report = json.loads((job_dir / "reports" / "prepare_article_report.json")
                        .read_text(encoding="utf-8"))
    rp0 = report["papers"][0]
    assert rp0["formal_paper_dir"].endswith(pid)
    assert "article" in rp0["article_dir"] and number in rp0["article_dir"]


def test_build_tex_notes_summary_cite_key_from_job_local(tmp_path):
    """build_tex prompt 的 notes summary cite key 来自 job-local metadata（jm 正确传递）。

    用非默认 JobManager(write_dir=tmp) 构造完整 job，build_tex 后
    04_tex_writing_prompt.md 的 notes summary 中 \\cite{} 非空且等于
    job-local metadata.citation_key。
    """
    papers_dir = tmp_path / "data" / "papers"
    paper_id = "2024_wang_texsummary"
    number = "0000000000000010"
    ckey = "wang2024texsummary"
    _make_formal_paper(papers_dir, paper_id, number, citation_key=ckey)

    jm, job_id = _make_job(tmp_path)
    catalog = FakeCatalog([{"paper_id": paper_id, "paper_number": number}])
    confirm_selected_papers(job_id, [{"paper_id": paper_id}], jm=jm, catalog=catalog)
    prepare_workset(job_id, jm=jm, papers_dir=papers_dir, catalog=catalog)
    deep_read(job_id, jm=jm, catalog=catalog)  # 笔记模板含 \cite{ckey}

    # build_tex 非 template-only 要求 deep_read_notes_filled + story_plan_filled。
    # 直接置位 + 写最小 story 文件，避免 LLM 回填。
    jdir = jm.job_dir(job_id)
    jm.set_step(job_id, "deep_read_notes_filled", True)
    (jdir / "planning" / "story_plan.md").write_text(
        "# story\n## scientific_background\nfilled\n", encoding="utf-8")
    (jdir / "planning" / "chapter_outline.md").write_text("# outline\n", encoding="utf-8")
    jm.set_step(job_id, "story_plan_filled", True)

    info = build_tex(job_id, jm=jm)
    prompt = Path(info["prompt_path"]).read_text(encoding="utf-8")
    cites = re.findall(r"\\cite\{([^}]*)\}", prompt)
    assert ckey in cites, f"prompt cite keys {cites} 缺少 job-local key {ckey}"
    # references.bib 与 prompt notes summary 共用同一 key
    bib = Path(info["references_bib"]).read_text(encoding="utf-8")
    assert f"@article{{{ckey}," in bib


def test_workset_manifest_uses_job_relative_paths(tmp_path):
    """workset_manifest.json 的 work_dir 必须是 job-relative；移动 job 后仍可解析。"""
    import shutil
    from src.writer.bib_manager import load_workset_manifest, job_local_bib_keys

    papers_dir = tmp_path / "data" / "papers"
    paper_id = "2024_wang_relpath"
    number = "0000000000000020"
    _make_formal_paper(papers_dir, paper_id, number, citation_key="wang2024relpath")

    jm, job_id = _make_job(tmp_path)
    catalog = FakeCatalog([{"paper_id": paper_id, "paper_number": number}])
    confirm_selected_papers(job_id, [{"paper_id": paper_id}], jm=jm, catalog=catalog)
    prepare_workset(job_id, jm=jm, papers_dir=papers_dir, catalog=catalog)

    manifest = json.loads((jm.job_dir(job_id) / "planning" / "workset_manifest.json")
                          .read_text(encoding="utf-8"))
    wd = manifest["copied"][0]["work_dir"]
    assert wd == f"article/{number}"
    assert not Path(wd).is_absolute()
    # manifest 不得泄漏绝对路径 / tmp_path。
    assert str(tmp_path) not in json.dumps(manifest)

    # 移动整个 job_dir 到新位置；基于相对 work_dir 仍能解析 article metadata。
    new_write = tmp_path / "moved" / "jobs"
    new_write.mkdir(parents=True)
    shutil.move(str(jm.job_dir(job_id)), str(new_write / job_id))
    jm2 = JobManager(write_dir=new_write)
    keys = job_local_bib_keys(load_workset_manifest(job_id, jm2))
    assert keys[paper_id] == "wang2024relpath"
