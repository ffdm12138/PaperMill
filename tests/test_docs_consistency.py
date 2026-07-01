"""Documentation consistency checks for active project boundaries."""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_readme_links_to_core_docs_exist():
    readme = _read("README.md")
    for rel in [
        "AGENTS.md",
        "CLAUDE.md",
        "docs/PROJECT_STATUS.md",
        "docs/PROJECT_CONTRACT.md",
        "docs/DEPENDENCIES_AND_EXTERNAL_TOOLS.md",
        "docs/WRITING_QUALITY_ACCEPTANCE.md",
        "docs/WRITER_PRODUCTIZATION_PLAN.md",
    ]:
        assert (ROOT / rel).exists(), f"README links to missing document: {rel}"
        assert rel in readme, f"README does not link to {rel}"


_BOUNDARY_TERMS = [
    "ingest-v2.2",
    "writing-v0.1",
    "conda run -n mineru",
    "metadata",
    "catalog",
    "write/jobs",
    "RAG",
    "Sci-Hub",
]


def test_agents_md_contains_boundary_terms():
    text = _read("AGENTS.md")
    for term in _BOUNDARY_TERMS:
        assert term in text, f"AGENTS.md missing term: {term}"
    assert ("unsafe optional" in text) or ("default disabled" in text)


def test_claude_md_contains_boundary_terms():
    text = _read("CLAUDE.md")
    for term in _BOUNDARY_TERMS:
        assert term in text, f"CLAUDE.md missing term: {term}"
    assert ("unsafe optional" in text) or ("default disabled" in text)


def test_project_status_covers_required_terms():
    text = _read("docs/PROJECT_STATUS.md")
    for term in ["ingest-v2.2", "writing-v0.1", "data/paper_raw", "data/papers", "write/jobs"]:
        assert term in text, f"PROJECT_STATUS.md missing term: {term}"


def test_project_contract_covers_required_terms():
    text = _read("docs/PROJECT_CONTRACT.md")
    for term in ["content-only", "metadata", "catalog", "DOI", "RAG", "embedding", "vector DB"]:
        assert term in text, f"PROJECT_CONTRACT.md missing term: {term}"


def test_dependencies_doc_covers_required_terms():
    text = _read("docs/DEPENDENCIES_AND_EXTERNAL_TOOLS.md")
    for term in [
        "mineru[all]",
        "PyMuPDF",
        "Crossref",
        "OpenAlex",
        "Unpaywall",
        "Sci-Hub",
        "unsafe optional",
        "ChromaDB",
        "sentence-transformers",
        "RAG",
    ]:
        assert term in text, f"DEPENDENCIES_AND_EXTERNAL_TOOLS.md missing term: {term}"


def test_docs_cover_mineru_gpu_conversion_sop():
    docs = [
        "README.md",
        "AGENTS.md",
        "CLAUDE.md",
        "docs/PROJECT_STATUS.md",
        "docs/PROJECT_CONTRACT.md",
        "docs/DEPENDENCIES_AND_EXTERNAL_TOOLS.md",
        "docs/MINERU_PERFORMANCE_PLAN.md",
        "skills/literature_library_manager/SKILL.md",
        "skills/literature_library_manager/CLAUDE.md",
    ]
    text = "\n".join(_read(rel) for rel in docs)
    for term in [
        "MINERU_REQUIRE_GPU=true",
        "CUDA_VISIBLE_DEVICES=0",
        "MINERU_RUNNER=cli_api_proxy",
        "MINERU_API_URL=http://127.0.0.1:8000",
        "MINERU_ALLOW_CPU=true",
        "scripts/convert_paper_raw_gpu.py",
        "torch.cuda.is_available",
    ]:
        assert term in text, f"GPU SOP docs missing term: {term}"
    assert (
        "MinerU conversion requires GPU" in text
        or "MinerU 正式转换必须使用 GPU" in text
    )
    assert "stage_raw_pdfs_to_paper_raw.py` 不需要 GPU" in text
    assert "convert_paper_raw_batch.py" in text
    assert "lower-level" in text or "底层" in text


def test_readme_does_not_list_write_jobs_as_committable():
    text = _read("README.md")
    assert "write/jobs" in text or ".gitkeep" in text
    for forbidden in ["提交 write/jobs", "提交 `write/jobs", "write/jobs 入库"]:
        assert forbidden not in text


def test_project_contract_does_not_encourage_rag_or_embedding():
    text = _read("docs/PROJECT_CONTRACT.md").lower()
    for forbidden in [
        "use rag",
        "enable embedding",
        "引入 rag",
        "启用 embedding",
        "引入 embedding",
        "启用 rag",
    ]:
        assert forbidden not in text


def test_active_docs_only_recommend_write_jobs_article_path():
    docs = [
        "README.md",
        "AGENTS.md",
        "CLAUDE.md",
        "write/README.md",
        "docs/PROJECT_CONTRACT.md",
        "docs/PROJECT_STATUS.md",
        "docs/ARCHITECTURE.md",
        "docs/WRITER_PRODUCTIZATION_PLAN.md",
        "skills/catalog_tex_writer/SKILL.md",
        "skills/literature_library_manager/SKILL.md",
    ]
    text = "\n".join(_read(rel) for rel in docs)
    assert "write/jobs/<job_id>/article/<paper_number>/" in text
    forbidden_tokens = [
        "data/llm_work",
        "write/<job",
        "write/{job",
        "global references.bib",
        "全局 references.bib",
        "从全局 references.bib 抽取",
        "catalog.metadata",
        "copy_paper_to_llm_work",
    ]
    offenders = [token for token in forbidden_tokens if token in text]
    assert not offenders, f"active docs still mention old workflow tokens: {offenders}"


def test_removed_legacy_writer_docs_are_absent():
    assert not (ROOT / "docs" / "LLM_USAGE_WORKFLOW.md").exists()
    assert not (ROOT / "skills" / "literature_review_writer").exists()


def test_docs_cover_formalize_state_machine():
    """v2.2 state machine: curate -> formalize -> commit, with formalize mandatory."""
    docs = [
        "README.md",
        "AGENTS.md",
        "CLAUDE.md",
        "docs/PROJECT_STATUS.md",
        "docs/PROJECT_CONTRACT.md",
        "docs/ARCHITECTURE.md",
        "skills/literature_library_manager/SKILL.md",
    ]
    text = "\n".join(_read(rel) for rel in docs)
    for term in [
        "formalize_paper_raw.py",
        "ready_for_commit",
        "catalog_ready",
    ]:
        assert term in text, f"v2.2 state-machine docs missing term: {term}"
    # curate must no longer be described as renaming/committing
    assert "curate 不再改名" in text or "curate 不再 rename" in text or "不改名" in text

