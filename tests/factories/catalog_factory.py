from __future__ import annotations


def make_minimal_catalog(
    *, paper_number: str = "0000000000000001", paper_name: str = "",
    title_zh: str = "示例论文", markdown_name: str | None = None,
) -> dict:
    """Return a complete context-free Catalog v3.2 fixture."""
    del markdown_name
    paper_name = paper_name or f"2024_Doe_{title_zh}"
    return {
        "schema_version": "3.2", "paper_number": paper_number, "paper_name": paper_name,
        "content_identity": {"content_title_zh": title_zh, "research_domains": ["测试领域"], "document_language": "en"},
        "abstract": {"source": {"status": "not_found", "origin": "not_found", "language": None, "text": None, "source_ref": None}, "summary_zh": "根据全文生成的测试摘要。", "summary_generation_basis": "full_text", "one_sentence_zh": "本文验证完整内容档案契约。"},
        "research_context": {"background_zh": "测试背景。", "knowledge_gap_zh": "测试缺口。", "research_question_zh": "测试问题？", "objectives_zh": ["验证契约"]},
        "methods": {"overview_zh": "测试方法。", "method_types": ["契约测试"], "models_or_algorithms": [], "experimental_design_zh": None, "evaluation_metrics": [], "comparison_baselines": []},
        "data_and_study_design": {"data_sources": [], "study_region_or_objects_zh": None, "time_range": None, "spatial_or_temporal_resolution": None, "sample_or_case_description_zh": None},
        "key_findings": [{"finding_zh": "契约有效。", "importance_zh": "防止职责漂移。", "evidence_refs": []}],
        "mechanisms": [{"description_zh": "通过 schema 门禁保持结构。", "source": "model", "evidence_refs": []}],
        "limitations": [{"description_zh": "仅为合成夹具。", "source": "authors", "evidence_refs": []}],
        "figures_and_tables": [],
        "terminology": {"items": [{"term_en": "contract", "term_zh": "契约", "definition_zh": "机器可验证的数据约束。"}], "not_applicable_reason": None},
        "writing_value": {"use_cases": ["契约测试"], "claims_supported": [], "suitable_sections": [], "comparison_value_zh": None, "cautions_zh": []},
        "screening": {"read_decision": "pending", "priority": None, "reason_zh": None},
        "provenance": {"metadata_sha256": "a", "metadata_freeze_sha256": "b", "markdown_sha256": "c", "conversion_manifest_sha256": "d", "catalog_task_sha256": "e", "image_hashes": {}, "source_record_hashes": {}, "skill_version": "paper_raw_catalog_curator.v3.2", "generated_at": "2026-01-01T00:00:00+08:00"},
    }
