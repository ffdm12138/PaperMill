from __future__ import annotations

from copy import deepcopy

from src.services.v2_library import empty_catalog


def make_minimal_catalog(
    *,
    paper_number: str = "0000000000000001",
    paper_id: str = "",
    title_zh: str = "示例论文",
    markdown_name: str | None = None,
) -> dict:
    """Return a minimal content-only catalog v3.1 object."""
    catalog = deepcopy(empty_catalog())
    markdown = markdown_name or f"{paper_number}.md"
    catalog["library_locator"].update(
        {
            "paper_number": paper_number,
            "paper_id": paper_id,
            "paper_dir": "",
        }
    )
    catalog["library_locator"]["asset_refs"].update(
        {
            "markdown": markdown,
            "pdf": f"{paper_number}.pdf",
            "metadata": f"{paper_number}.metadata.json",
            "catalog": f"{paper_number}.catalog.json",
            "asset_manifest": f"{paper_number}.asset_manifest.json",
            "images_dir": "images/",
        }
    )
    catalog["content_identity"].update(
        {
            "content_title_zh": title_zh,
            "content_title_original": "Example Paper",
            "content_title_original_source": "test",
            "content_title_original_candidates": ["Example Paper"],
            "content_language": "en",
            "document_type": "article",
        }
    )
    catalog["classification"]["primary_domain"] = "test_domain"
    catalog["terminology"] = [
        {
            "term_original": "example",
            "term_zh": "示例",
            "abbr": "",
            "note_zh": "测试用最小术语。",
        }
    ]
    catalog["screening"].update(
        {
            "read_decision": "pending",
            "relevance_score": 4,
            "novelty_score": 3,
            "method_quality_score": 4,
            "priority_score": 4,
            "reason": "该文献用于测试内容型 catalog 契约。",
            "recommended_next_action": "read",
        }
    )
    catalog["research_card"].update(
        {
            "research_problem": "测试研究问题。",
            "core_question": "测试核心问题。",
            "hypothesis_or_objective": "测试研究目标。",
            "study_object": "测试对象。",
            "method_summary": "测试方法。",
            "data_or_experiment": "测试数据。",
            "main_findings": ["测试发现。"],
            "mechanisms": ["测试机制。"],
            "limitations": ["测试局限。"],
            "usefulness_for_user": "测试用途。",
        }
    )
    catalog["writing_value"]["short_summary"] = "这是一条最小合法测试摘要。"
    catalog["quality_control"].update(
        {
            "catalog_completeness": "complete",
            "missing_fields": [],
            "warnings": [],
            "is_test_fixture": False,
            "fallback_used": False,
        }
    )
    catalog["provenance"].update(
        {
            "markdown_path": markdown,
            "metadata_path": f"{paper_number}.metadata.json",
            "asset_manifest_path": f"{paper_number}.asset_manifest.json",
            "generated_at": "2026-01-01T00:00:00",
            "generator": "tests.factories",
            "generator_version": "1.0",
            "generation_mode": "script_only",
        }
    )
    return catalog
