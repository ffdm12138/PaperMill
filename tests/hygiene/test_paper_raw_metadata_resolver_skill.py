"""Tests for the paper_raw_metadata_resolver skill files and boundary docs."""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ROOT = _REPO_ROOT / "skills" / "paper_raw_metadata_resolver"
_PATCH_SCHEMA_PATH = _ROOT / "metadata_patch_schema.json"
_PATCH_EXAMPLE_PATH = _ROOT / "examples" / "example_metadata_patch.json"


def test_resolver_skill_files_exist():
    for name in [
        "SKILL.md",
        "README.md",
        "CLAUDE.md",
        "metadata_candidate_schema.json",
        "metadata_patch_schema.json",
        "examples/example_input.md",
        "examples/example_candidates.json",
        "examples/example_metadata_patch.json",
    ]:
        assert (_ROOT / name).exists(), f"missing skill file: {name}"


def test_resolver_skill_has_frontmatter():
    text = (_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "name: paper_raw_metadata_resolver" in text
    assert "description:" in text


def test_resolver_skill_documents_boundaries():
    text = (_ROOT / "SKILL.md").read_text(encoding="utf-8").lower()
    # input only from paper_raw
    assert "data/paper_raw" in text
    assert "metadata.json" in text
    # never read formal sources
    assert "data/papers" in text
    assert "never read" in text or "不得读取" in text or "不得读" in text
    # never fabricate DOI
    assert "doi" in text
    assert "fabricate" in text or "编造" in text
    # LLM-guessed DOI is invalid
    assert "llm" in text and ("invalid" in text or "无效" in text)
    # must NOT write match authority
    assert "match receipt" in text
    assert "不得" in text or "must not" in text or "must not set" in text
    # outputs candidates + patch only
    assert "candidates" in text and "patch" in text


def test_resolver_skill_requires_converted_md_and_online_verification():
    text = (_ROOT / "SKILL.md").read_text(encoding="utf-8")
    tl = text.lower()
    # converted Markdown is the primary evidence
    assert "转换" in text or "converted" in tl
    assert "markdown" in tl
    # verify online / search online
    assert "联网验证" in text or "verify" in tl
    assert "联网查询" in text or "search" in tl
    # same schema as network-fetched metadata
    assert "同一 schema" in text or "same schema" in tl or "结构同" in text
    # never fabricate
    assert "不得编造" in text or "never fabricate" in tl
    assert "unresolved" in tl or "no_candidates" in tl


def test_resolver_skill_does_not_set_match_status():
    text = (_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert "match receipt" in text
    assert "不得" in text or "must not" in text.lower()


def test_candidate_schema_requires_doi_pattern():
    schema = json.loads((_ROOT / "metadata_candidate_schema.json").read_text(encoding="utf-8"))
    assert schema["type"] == "object"
    cand_item = schema["properties"]["candidates"]["items"]
    doi_prop = cand_item["properties"]["doi"]
    assert "pattern" in doi_prop
    assert doi_prop["pattern"].startswith("^10")
    rec = schema["properties"]["recommendation"]["properties"]["decision"]
    assert "auto_matched" in rec["enum"]
    assert "manual_review" in rec["enum"]


def test_patch_schema_shares_curator_shape():
    schema = json.loads((_ROOT / "metadata_patch_schema.json").read_text(encoding="utf-8"))
    props = schema["properties"]
    for key in ("title", "authors", "year", "container", "publication", "identifiers", "links"):
        assert key in props, f"patch schema missing {key}"
    assert "doi" in props["identifiers"]["properties"]


def test_examples_are_valid_json():
    cands = json.loads((_ROOT / "examples/example_candidates.json").read_text(encoding="utf-8"))
    assert "candidates" in cands and "recommendation" in cands
    patch = json.loads((_ROOT / "examples/example_metadata_patch.json").read_text(encoding="utf-8"))
    assert "identifiers" in patch and patch["identifiers"]["doi"]


def test_resolver_patch_example_uses_network_metadata_shape():
    """The resolver patch must use the same nested shape as network-fetched
    metadata (empty_metadata subset), not a simplified {title, doi, authors}旁路."""
    patch = json.loads((_ROOT / "examples/example_metadata_patch.json").read_text(encoding="utf-8"))
    # nested bibliographic structure, matching empty_metadata
    assert isinstance(patch.get("title"), dict) and patch["title"].get("original")
    assert isinstance(patch.get("authors"), list) and patch["authors"][0].get("family")
    assert isinstance(patch.get("container"), dict) and patch["container"].get("journal")
    assert isinstance(patch.get("publication"), dict) and patch["publication"].get("volume")
    assert isinstance(patch.get("identifiers"), dict) and patch["identifiers"].get("doi")
    # no simplified top-level doi/authors-as-string旁路
    assert not isinstance(patch.get("doi"), str)
    assert not isinstance(patch.get("authors"), str)


def test_resolver_skill_manual_path_convert_before_resolve():
    """SKILL.md must state the manual PDF ordering: convert before resolve."""
    text = (_ROOT / "SKILL.md").read_text(encoding="utf-8")
    tl = text.lower()
    # manual path: convert first, resolve second
    assert "先转换" in text or "convert" in tl
    # primary evidence = converted Markdown
    assert "primary evidence" in tl or "候选主证据" in text or "converted" in tl
    # online verify / search
    assert "联网验证" in text or "verify online" in tl
    assert "联网查询" in text or "search online" in tl
    # fail closed
    assert "fail" in tl
    # same schema
    assert "同一 schema" in text or "same schema" in tl
    # never fabricate
    assert "不得编造" in text or "never fabricate" in tl
    # do not run before MinerU conversion
    assert ("mineru" in tl and "转换" in text) or "conversion" in tl


def test_resolver_skill_status_permission():
    text = (_ROOT / "SKILL.md").read_text(encoding="utf-8")
    tl = text.lower()
    assert "llm-facing" in tl
    assert "match receipt" in tl
    assert "不得" in text and "independent" in tl


def test_metadata_resolver_service_docstring_status_authority():
    """Service docstring must distinguish LLM candidate generation from apply stamping."""
    text = (_REPO_ROOT / "src" / "metadata_resolve" / "resolver.py").read_text(encoding="utf-8")
    head = text.split('"""', 2)[1]
    assert "converted Markdown is the primary evidence" in head
    assert "optional hints" in head
    assert "never" in head and "authoritative match receipt" in head
    assert "independent pdf identity extraction" in head.lower()


def test_resolver_skill_mentions_markdown_first_100_before_pdf_fallback():
    text = (_ROOT / "SKILL.md").read_text(encoding="utf-8").lower()
    assert "read converted markdown first 100 lines for title/author candidates before pdf title fallback" in text


# ── Patch schema: example conformance + legacy field rejection ─────────


@pytest.fixture(scope="module")
def _patch_schema() -> dict:
    return json.loads(_PATCH_SCHEMA_PATH.read_text(encoding="utf-8"))


def test_example_metadata_patch_conforms_to_schema(_patch_schema: dict):
    """The bundled example patch must validate against the patch schema."""
    example = json.loads(_PATCH_EXAMPLE_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(instance=example, schema=_patch_schema)
    # jsonschema.validate raises on failure; reaching here means the example is valid.


def test_patch_schema_rejects_title_short_zh(_patch_schema: dict):
    payload = {"title": {"original": "x", "short_zh": "旧"}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=_patch_schema)


def test_patch_schema_rejects_title_translated_zh(_patch_schema: dict):
    payload = {"title": {"original": "x", "translated_zh": "旧"}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=_patch_schema)


def test_patch_schema_rejects_source_raw_record(_patch_schema: dict):
    payload = {"source": {"raw_record": {"x": 1}}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=_patch_schema)


def test_patch_schema_rejects_metadata_match(_patch_schema: dict):
    payload = {"metadata_match": {"status": "unmatched"}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=_patch_schema)


@pytest.mark.parametrize("key,value", [
    ("abstract", "x"),
    ("keywords", ["x"]),
    ("notes", "x"),
])
def test_patch_schema_rejects_top_level_legacy_fields(_patch_schema: dict, key: str, value):
    payload = {"title": {"original": "x"}, key: value}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=_patch_schema)
