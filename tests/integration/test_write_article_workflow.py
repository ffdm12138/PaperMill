import argparse
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.check_write_tex_project import check_tex_project
from scripts.prepare_write_article_workdir import prepare_workdir
from scripts.write_catalog_tex_article import write_article
from src.services.v2_library import empty_catalog

POST_TRIAGE_MUST_READ = "must_" + "read"


def _metadata(idx: int) -> dict:
    return {
        "citation_key": f"paper{idx}2024",
        "entry_type": "article",
        "title": {"original": f"Workflow Paper {idx}"},
        "authors": [{"full_name": f"Author {idx}", "family": f"Author{idx}", "given": "A"}],
        "year": 2024,
        "container": {"journal": "Workflow Journal", "conference": "", "booktitle": "", "publisher": "Test Press"},
        "publication": {"volume": "1", "number": str(idx), "issue": str(idx), "pages": f"{idx}-{idx + 10}", "article_number": ""},
        "identifiers": {"doi": f"10.1234/workflow.{idx}"},
        "links": {"url": f"https://example.org/{idx}"},
    }


def _catalog(idx: int, *, primary_domain: str = "snow_model", topic: str = "blowing snow") -> dict:
    paper_number = f"{idx:016d}"
    paper_id = f"2024_author{idx}_workflow_paper_{idx}"
    catalog = empty_catalog()
    catalog["library_locator"].update({"paper_number": paper_number, "paper_id": paper_id})
    catalog["library_locator"]["paper_dir"] = f"data/papers/{paper_id}"
    catalog["content_identity"].update({
        "content_title_zh": f"流程论文{idx}",
        "content_title_original": f"Workflow Paper {idx}",
        "content_title_original_candidates": [f"Workflow Paper {idx}"],
        "content_language": "en",
        "document_type": "article",
    })
    catalog["classification"].update({
        "primary_domain": primary_domain,
        "secondary_domains": [primary_domain],
        "topic_tags": [topic],
    })
    catalog["screening"].update({
        "read_decision": "pending",
        "relevance_score": idx,
        "novelty_score": None,
        "method_quality_score": None,
        "reason_zh": "",
    })
    catalog["research_card"].update({
        "research_problem_zh": f"论文{idx}提供写作流程测试证据",
        "method_summary_zh": f"方法{idx}",
        "main_findings_zh": [f"结论{idx}"],
        "usefulness_for_user_zh": f"用途{idx}",
    })
    catalog["writing_value"].update({
        "short_summary": f"论文{idx}提供写作流程测试证据",
        "possible_use_in_writing": [
            {"use_case": "introduction", "description_zh": "用于引言背景。"},
            {"use_case": "discussion", "description_zh": "用于讨论对比。"},
        ],
    })
    catalog["terminology"] = [{"term_original": "blowing snow", "term_zh": "风吹雪"}]
    catalog["quality_control"].update({"catalog_completeness": "partial"})
    catalog["provenance"].update({
        "generated_from": "mineru_markdown",
        "markdown_path": f"{paper_id}.md",
        "generated_at": "2026-07-03T00:00:00",
        "generator": "test",
    })
    return catalog


def _all_catalog_entry(catalog: dict, paper_id: str, paper_number: str) -> dict:
    return {
        "paper_number": paper_number,
        "paper_id": paper_id,
        "paper_dir": f"data/papers/{paper_id}",
        "asset_refs": catalog["library_locator"]["asset_refs"],
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
    }


def _make_library(tmp_path: Path, count: int = 3) -> tuple[Path, Path, Path, list[dict]]:
    papers_dir = tmp_path / "data" / "papers"
    catalog_path = tmp_path / "data" / "catalog" / "all.catalog.json"
    entries = []
    for idx in range(1, count + 1):
        paper_number = f"{idx:016d}"
        paper_id = f"2024_author{idx}_workflow_paper_{idx}"
        folder = papers_dir / paper_id
        (folder / "images").mkdir(parents=True)
        metadata = _metadata(idx)
        catalog = _catalog(idx)
        (folder / f"{paper_id}.metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        (folder / f"{paper_id}.catalog.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
        (folder / f"{paper_id}.md").write_text(f"# Workflow Paper {idx}\n\nbody", encoding="utf-8")
        (folder / f"{paper_id}.pdf").write_bytes(b"%PDF-test")
        entries.append(_all_catalog_entry(catalog, paper_id, paper_number))
    catalog_path.parent.mkdir(parents=True)
    catalog_path.write_text(json.dumps({"schema_version": "3.1", "papers": entries}, ensure_ascii=False), encoding="utf-8")
    write_dir = tmp_path / "write" / "jobs"
    return catalog_path, papers_dir, write_dir, entries


def _prepare_args(catalog_path: Path, papers_dir: Path, write_dir: Path, **kwargs) -> argparse.Namespace:
    defaults = {
        "job_id": "job_article",
        "paper_numbers": None,
        "primary_domain": None,
        "topic": None,
        "read_decision": None,
        "min_relevance_score": None,
        "limit": None,
        "apply": True,
        "dry_run": False,
        "overwrite": False,
        "all_catalog": catalog_path,
        "papers_dir": papers_dir,
        "write_dir": write_dir,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_prepare_by_paper_numbers_copies_and_writes_report(tmp_path):
    catalog_path, papers_dir, write_dir, entries = _make_library(tmp_path)
    args = _prepare_args(
        catalog_path,
        papers_dir,
        write_dir,
        paper_numbers=[e["paper_number"] for e in entries],
    )

    report = prepare_workdir(args)

    assert report["selected_count"] == 3
    job_dir = write_dir / "job_article"
    assert (job_dir / "selected_catalog.json").exists()
    assert (job_dir / "reports" / "prepare_article_report.json").exists()
    selected = json.loads((job_dir / "selected_catalog.json").read_text(encoding="utf-8"))
    # selected_catalog entries carry flat content fields only; bibliographic
    # metadata lives in the copied article/<paper_number>/*.metadata.json.
    # all.catalog itself remains content-only & separated.
    p0 = selected["papers"][0]
    assert "classification" in p0 and "screening" in p0 and "research_card" in p0
    assert "metadata" not in p0  # content-only snapshot; metadata is in article/
    assert "catalog" not in p0
    for entry in entries:
        copied = job_dir / "article" / entry["paper_number"] / f"{entry['paper_id']}.metadata.json"
        assert copied.exists()


def test_prepare_by_catalog_filter(tmp_path):
    catalog_path, papers_dir, write_dir, entries = _make_library(tmp_path)
    # This fixture represents post-triage annotations, not initial generated catalog.
    for entry in entries[1:]:
        entry["screening"]["read_decision"] = POST_TRIAGE_MUST_READ
    catalog_path.write_text(json.dumps({"schema_version": "3.1", "papers": entries}, ensure_ascii=False), encoding="utf-8")
    args = _prepare_args(
        catalog_path,
        papers_dir,
        write_dir,
        job_id="filter_job",
        primary_domain="snow_model",
        topic="blowing",
        read_decision=POST_TRIAGE_MUST_READ,
        min_relevance_score=2,
        limit=2,
    )

    report = prepare_workdir(args)

    assert report["selected_count"] == 2
    numbers = [p["paper_number"] for p in report["papers"]]
    assert numbers == ["0000000000000003", "0000000000000002"]


def test_prepare_missing_paper_number_does_not_create_job(tmp_path):
    catalog_path, papers_dir, write_dir, _ = _make_library(tmp_path)
    args = _prepare_args(catalog_path, papers_dir, write_dir, paper_numbers=["9999999999999999"])

    with pytest.raises(KeyError):
        prepare_workdir(args)

    assert not (write_dir / "job_article").exists()


def test_prepare_rejects_existing_job_without_overwrite(tmp_path):
    catalog_path, papers_dir, write_dir, entries = _make_library(tmp_path)
    args = _prepare_args(catalog_path, papers_dir, write_dir, paper_numbers=[e["paper_number"] for e in entries])
    prepare_workdir(args)

    with pytest.raises(FileExistsError):
        prepare_workdir(args)


@pytest.mark.parametrize("forbidden", ["raw", "paper_raw", "llm_work"])
def test_prepare_rejects_forbidden_source_dirs(tmp_path, forbidden):
    catalog_path, _, write_dir, entries = _make_library(tmp_path)
    forbidden_dir = tmp_path / "data" / forbidden
    for entry in entries:
        source = tmp_path / "data" / "papers" / entry["paper_id"]
        target = forbidden_dir / entry["paper_id"]
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)

    args = _prepare_args(
        catalog_path,
        forbidden_dir,
        write_dir,
        paper_numbers=[entries[0]["paper_number"]],
    )

    with pytest.raises(ValueError, match="formal papers dir"):
        prepare_workdir(args)


def test_tex_article_generation_and_check_success(tmp_path):
    catalog_path, papers_dir, write_dir, entries = _make_library(tmp_path)
    prepare_workdir(_prepare_args(catalog_path, papers_dir, write_dir, paper_numbers=[e["paper_number"] for e in entries]))

    write_report = write_article(argparse.Namespace(
        job_id="job_article",
        title="Workflow Mini Article",
        language="zh",
        apply=True,
        dry_run=False,
        overwrite=False,
        write_dir=write_dir,
    ))
    check_report = check_tex_project(argparse.Namespace(job_id="job_article", compile=False, write_dir=write_dir))

    assert write_report["paper_count"] == 3
    assert (write_dir / "job_article" / "tex" / "main.tex").exists()
    assert (write_dir / "job_article" / "tex" / "references.bib").exists()
    assert check_report["valid"] is True
    assert check_report["bib_count"] == 3


def test_write_catalog_tex_article_requires_three_doi_papers(tmp_path):
    catalog_path, papers_dir, write_dir, entries = _make_library(tmp_path)
    prepare_workdir(_prepare_args(catalog_path, papers_dir, write_dir, paper_numbers=[e["paper_number"] for e in entries]))
    first = write_dir / "job_article" / "article" / entries[0]["paper_number"] / f"{entries[0]['paper_id']}.metadata.json"
    metadata = json.loads(first.read_text(encoding="utf-8"))
    metadata["identifiers"]["doi"] = ""
    first.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="metadata.identifiers.doi"):
        write_article(argparse.Namespace(
            job_id="job_article",
            title="Workflow Mini Article",
            language="zh",
            apply=True,
            dry_run=False,
            overwrite=False,
            write_dir=write_dir,
        ))


def test_check_catches_missing_bib_key_data_papers_path_and_missing_doi(tmp_path):
    catalog_path, papers_dir, write_dir, entries = _make_library(tmp_path)
    prepare_workdir(_prepare_args(catalog_path, papers_dir, write_dir, paper_numbers=[e["paper_number"] for e in entries]))
    write_article(argparse.Namespace(
        job_id="job_article",
        title="Workflow Mini Article",
        language="zh",
        apply=True,
        dry_run=False,
        overwrite=False,
        write_dir=write_dir,
    ))

    tex_dir = write_dir / "job_article" / "tex"
    main_path = tex_dir / "main.tex"
    main_path.write_text(main_path.read_text(encoding="utf-8") + "\n\\cite{missingkey}\n% data/papers/bad\n", encoding="utf-8")
    bib_path = tex_dir / "references.bib"
    bib_path.write_text(
        bib_path.read_text(encoding="utf-8").replace("  doi = {10.1234/workflow.1},\n", ""),
        encoding="utf-8",
    )

    report = check_tex_project(argparse.Namespace(job_id="job_article", compile=False, write_dir=write_dir))

    assert report["valid"] is False
    assert any("missingkey" in error for error in report["errors"])
    assert any("data/papers" in error for error in report["errors"])
    assert any("missing nonempty doi" in error for error in report["errors"])


@pytest.mark.parametrize("restricted", ["data/paper_raw/bad", "data/raw/bad", "data/llm_work/bad"])
def test_check_rejects_restricted_tex_paths(tmp_path, restricted):
    catalog_path, papers_dir, write_dir, entries = _make_library(tmp_path)
    prepare_workdir(_prepare_args(catalog_path, papers_dir, write_dir, paper_numbers=[e["paper_number"] for e in entries]))
    write_article(argparse.Namespace(
        job_id="job_article",
        title="Workflow Mini Article",
        language="zh",
        apply=True,
        dry_run=False,
        overwrite=False,
        write_dir=write_dir,
    ))
    main_path = write_dir / "job_article" / "tex" / "main.tex"
    main_path.write_text(main_path.read_text(encoding="utf-8") + f"\n% {restricted}\n", encoding="utf-8")

    report = check_tex_project(argparse.Namespace(job_id="job_article", compile=False, write_dir=write_dir))

    assert report["valid"] is False
    assert any(restricted.split("/")[1] in error for error in report["errors"])
