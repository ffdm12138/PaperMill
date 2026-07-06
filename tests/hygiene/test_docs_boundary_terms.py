"""Documentation boundary checks for active project contracts.

Slimmed to enforce stable, contract-level doc guarantees. Pure keyword-lockdown
scans (e.g. "AGENTS.md must contain term X") and the duplicate AGENTS==CLAUDE
guard (already in test_docs_alignment.py) were removed; resolver/version/script
guards were consolidated.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _active_markdown_paths() -> list[Path]:
    paths = [
        ROOT / "README.md",
        ROOT / "AGENTS.md",
        ROOT / "CLAUDE.md",
        *sorted((ROOT / "docs").rglob("*.md")),
        *sorted((ROOT / "skills").rglob("*.md")),
        *sorted((ROOT / "scripts").rglob("*.md")),
    ]
    return [path for path in paths if path.exists()]


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


def test_docs_cover_metadata_only_pdf_fetch_contract():
    text = "\n".join(_read(rel) for rel in [
        "README.md",
        "AGENTS.md",
        "CLAUDE.md",
        "docs/PROJECT_CONTRACT.md",
        "docs/DEPENDENCIES_AND_EXTERNAL_TOOLS.md",
        "docs/ARCHITECTURE.md",
        "skills/literature_library_manager/SKILL.md",
    ])
    for term in [
        "Metadata-only",
        "paper_raw",
        "doi.csv",
        "<paper_number>.pdf",
        "header-based",
        "User-Agent",
        "header values",
    ]:
        assert term in text, f"metadata-only PDF fetch docs missing term: {term}"


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
    for term in [
        "/health",
        "liveness only",
        "READY_FOR_CONVERSION",
        "smoke_mineru_conversion.py",
        "--restart-if-stale",
    ]:
        assert term in text, f"GPU readiness SOP docs missing term: {term}"
    assert (
        "MinerU conversion requires GPU" in text
        or "MinerU 正式转换必须使用 GPU" in text
    )
    assert "stage_raw_pdfs_to_paper_raw.py` 不需要 GPU" in text
    assert "convert_paper_raw_batch.py" in text
    assert "lower-level" in text or "底层" in text


def test_docs_cover_conversion_metadata_layering():
    """Conversion layer must NOT require metadata; formalize/commit must.
    Docs must state this layered semantics explicitly and must not claim the
    reverse ('must match metadata before conversion')."""
    docs = [
        "README.md",
        "AGENTS.md",
        "CLAUDE.md",
        "docs/PROJECT_STATUS.md",
        "docs/PROJECT_CONTRACT.md",
        "docs/ARCHITECTURE.md",
        "skills/literature_library_manager/SKILL.md",
        "skills/literature_library_manager/CLAUDE.md",
    ]
    text = "\n".join(_read(rel) for rel in docs)
    # Conversion does not require metadata.
    assert (
        "conversion does not require" in text.lower()
        or "转换不要求 metadata" in text
        or "转换不要求完整 metadata" in text
        or "convert first is allowed" in text.lower()
    ), "docs must state conversion does not require metadata"
    # Conversion output Markdown is a metadata-resolution source.
    assert (
        "metadata-resolution source" in text.lower()
        or "metadata resolution source" in text.lower()
        or "metadata 解析" in text
    )
    # Formalize/commit requires strict metadata.
    assert (
        "formalize/commit requires strict metadata" in text.lower()
        or "commit requires metadata" in text.lower()
        or "formalize/commit" in text.lower()
    )
    # BibTeX from metadata, never catalog.
    assert "BibTeX" in text or "bibtex" in text.lower()
    # Forbidden reverse phrasing: must not claim metadata must be matched first.
    for forbidden in [
        "先 metadata matched 才能 convert",
        "必须先 metadata matched",
        "must match metadata before conversion",
        "metadata matched 才能 convert",
        "Network metadata path:** `--only-preflight-ready` is safe",
        "可安全使用\n`--only-preflight-ready`",
    ]:
        assert forbidden not in text, f"docs contain reverse layering phrasing: {forbidden}"
    assert "legacy/compatibility" in text


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
    """v2.3 state machine: curate -> formalize -> commit, with formalize mandatory."""
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


def test_initial_catalog_docs_do_not_request_final_read_decision():
    forbidden = "read_decision" + "(must_read/maybe_read/skip)"
    offenders = [
        str(path.relative_to(ROOT))
        for path in _active_markdown_paths()
        if forbidden in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_final_read_decision_docs_are_stage_qualified():
    qualifiers = [
        "writing-stage",
        "post-triage",
        "人工筛选",
        "精读 triage",
        "写作阶段",
        "后续",
        "禁止",
        "不得",
        "不要",
        "only",
    ]
    final_tokens = ["must_read", "maybe_read"]
    offenders: list[str] = []
    for path in _active_markdown_paths():
        text = path.read_text(encoding="utf-8")
        for token in final_tokens:
            start = 0
            while True:
                index = text.find(token, start)
                if index == -1:
                    break
                context = text[max(0, index - 180): index + 180]
                if "read_decision" in context and not any(q in context for q in qualifiers):
                    offenders.append(f"{path.relative_to(ROOT)}:{token}")
                start = index + len(token)
        start = 0
        while True:
            index = text.find("skip", start)
            if index == -1:
                break
            context = text[max(0, index - 180): index + 180]
            if "read_decision" in context and not any(q in context for q in qualifiers):
                offenders.append(f"{path.relative_to(ROOT)}:skip")
            start = index + len("skip")
    assert offenders == []


# ── positive guards: active docs must contain current schema terms ──

_ACTIVE_DOCS_CURRENT = [
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "docs/PROJECT_STATUS.md",
    "docs/PROJECT_CONTRACT.md",
    "docs/ARCHITECTURE.md",
    "docs/DEPENDENCIES_AND_EXTERNAL_TOOLS.md",
    "docs/PDF_RESOLVER_DESIGN.md",
    "docs/PDF_RESOLVER_INTEGRATION_PLAN.md",
]


def test_active_docs_state_current_schema_versions():
    """Active docs must declare catalog schema v3.1 and ingest v2.3 current state."""
    text = "\n".join(_read(rel) for rel in _ACTIVE_DOCS_CURRENT)
    assert "catalog schema 为 v3.1" in text or "catalog（schema v3.1）" in text or "catalog (schema v3.1)" in text
    assert "content-only catalog (v3.1)" in text
    assert "ingest v2.3 strict-only" in text.lower() or "ingest v2.3 current" in text.lower()


def test_readme_does_not_call_current_state_v2_2():
    """README must not refer to the current state machine as v2.2."""
    text = _read("README.md")
    assert "v2.2 状态机" not in text
    assert "v2.3 状态机" in text or "v2.3 strict-only" in text


def test_skills_catalog_curator_docs_state_v3_1():
    """Catalog curator skill docs must speak v3.1, not v2.0."""
    for rel in [
        "skills/paper_raw_catalog_curator/README.md",
        "skills/paper_raw_catalog_curator/SKILL.md",
        "skills/paper_raw_catalog_curator/CLAUDE.md",
    ]:
        text = _read(rel)
        assert "catalog（v2.0" not in text, f"{rel} still says catalog v2.0"
        assert "v2.0 content-only catalog" not in text, f"{rel} still says v2.0"
        assert "v3.1" in text, f"{rel} missing catalog v3.1"


def test_active_docs_pdf_resolver_contract():
    """Active docs must cover the current PDF resolver chain and must not
    retain the legacy 'configured header_based' rule.

    Consolidates: resolver terms (%PDF, unsafe final URL, FETCH_PROXY,
    header_based), resolver order (sciengine_direct), the doi.org default URL,
    and the forbidden 'configured header_based' phrasing.
    """
    text = "\n".join(_read(rel) for rel in _ACTIVE_DOCS_CURRENT)
    for term in ["%PDF", "unsafe final URL", "FETCH_PROXY", "header_based",
                 "sciengine_direct"]:
        assert term in text, f"active docs missing PDF resolver term: {term}"
    # AGENTS.md / CLAUDE.md (identical) must reflect the new resolver contract
    agents_text = _read("AGENTS.md")
    assert "https://doi.org/" in agents_text, "AGENTS.md missing doi.org default URL"
    # Forbidden legacy 'configured header_based' rule
    forbidden = [
        "configured header_based",
        "explicit header-based resolver if configured",
        "Without configuration, ``header_based`` never runs",
        "without configuration, ``header_based`` never runs",
        "otherwise surfaced as `not_configured_resolvers`",
        "仅 `--base-url`/`--url-template` 配置时",
    ]
    for s in forbidden:
        assert s not in text, f"active docs still contain forbidden rule: {s!r}"


def test_script_usage_covers_root_scripts():
    """docs/SCRIPT_USAGE.md must document every scripts/*.py and must not
    reference phantom scripts that don't exist.

    Consolidates: every root script documented + no phantom scripts."""
    docs = _read("docs/SCRIPT_USAGE.md")
    for path in sorted((ROOT / "scripts").glob("*.py")):
        assert path.name in docs, f"{path.name} missing from docs/SCRIPT_USAGE.md"
    assert "_patch_rich_fields.py" not in docs
    assert "_patch_rich_fields2.py" not in docs


def test_script_usage_doc_linked_from_readme_and_agent_docs():
    """SCRIPT_USAGE.md must be linked from README, AGENTS, and CLAUDE."""
    for rel in ["README.md", "AGENTS.md", "CLAUDE.md"]:
        assert "docs/SCRIPT_USAGE.md" in _read(rel), f"{rel} missing SCRIPT_USAGE.md link"


def test_agent_acceptance_exists_and_referenced():
    """agent_acceptance.py must exist and be referenced in docs."""
    assert (ROOT / "scripts" / "agent_acceptance.py").exists(), "agent_acceptance.py missing"
    agents_text = _read("AGENTS.md")
    assert "agent_acceptance.py" in agents_text, "AGENTS.md missing agent_acceptance.py reference"
    # AGENTS.md must require the canonical acceptance output strings so agents
    # report them verbatim (see CLAUDE.md §7).
    assert "[OK] agent acceptance passed" in agents_text, \
        "AGENTS.md missing required '[OK] agent acceptance passed' contract"
    assert "[OK] Packed: mineru_snapshot.zip" in agents_text, \
        "AGENTS.md missing required '[OK] Packed: mineru_snapshot.zip' contract"


def test_access_policy_oa_resolvers_include_sciengine_direct():
    """AccessPolicy._oa_resolvers() must include sciengine_direct."""
    from src.fetch.access_policy import AccessPolicy
    names = AccessPolicy().enabled_resolver_names()
    assert "sciengine_direct" in names, "AccessPolicy OA resolvers missing sciengine_direct"
