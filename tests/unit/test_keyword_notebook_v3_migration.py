from __future__ import annotations

import hashlib
import json

import pytest

from src.discovery.notebook_v3_migration import (
    LegacyQuery,
    MigrationBlocked,
    _empty_query,
    _legacy_queries,
    _merge_query,
    _translate_legacy_provider_state,
    inventory_notebooks,
    load_mapping_manifest,
)


def test_mapping_requires_explicit_confirmation(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    notebook = source / "old.json"
    notebook.write_text("{}", encoding="utf-8")
    digest = hashlib.sha256(notebook.read_bytes()).hexdigest()
    manifest = tmp_path / "mapping.json"
    manifest.write_text(json.dumps({
        "schema_version": "1.0",
        "mappings": [{
            "source_notebook": "old.json", "source_sha256": digest,
            "keyword_zh": "风吹雪", "status": "suggested",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(MigrationBlocked, match="not confirmed"):
        load_mapping_manifest(manifest, topics={"风吹雪": {}}, source_dir=source)


def test_mapping_unmapped_source_blocks(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    first = source / "first.json"
    second = source / "second.json"
    first.write_text("{}", encoding="utf-8")
    second.write_text("{}", encoding="utf-8")
    manifest = tmp_path / "mapping.json"
    manifest.write_text(json.dumps({
        "schema_version": "1.0",
        "mappings": [{
            "source_notebook": first.name,
            "source_sha256": hashlib.sha256(first.read_bytes()).hexdigest(),
            "keyword_zh": "风吹雪",
            "status": "confirmed",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(MigrationBlocked, match="exact source set"):
        load_mapping_manifest(manifest, topics={"风吹雪": {}}, source_dir=source)


def test_duplicate_source_mapping_blocks(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    notebook = source / "old.json"
    notebook.write_text("{}", encoding="utf-8")
    digest = hashlib.sha256(notebook.read_bytes()).hexdigest()
    manifest = tmp_path / "mapping.json"
    manifest.write_text(json.dumps({
        "schema_version": "1.0",
        "mappings": [
            {"source_notebook": notebook.name, "source_sha256": digest, "keyword_zh": "风吹雪", "status": "confirmed"},
            {"source_notebook": notebook.name, "source_sha256": digest, "keyword_zh": "风雪动力学", "status": "confirmed"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(MigrationBlocked, match="more than once"):
        load_mapping_manifest(
            manifest,
            topics={"风吹雪": {}, "风雪动力学": {}},
            source_dir=source,
        )


def test_inventory_reads_v3_provider_state_and_page_journal_count(tmp_path):
    notebook_dir = tmp_path / "keyword_notebooks"
    notebook_dir.mkdir()
    query = _empty_query(
        "示例主题", "zh", "pytest", active=True,
        migration_at="2026-01-01T00:00:00Z",
    )
    query["query_id"] = "query-1"
    query["providers"]["openalex"]["backfill"].update({
        "cursor": "opaque-cursor",
        "generation": 3,
        "request_signature": "signature-1",
        "generation_history": [{"generation": 2}],
    })
    notebook = {
        "schema_version": "3.0",
        "keyword_id": "keyword-1",
        "keyword_zh": "示例主题",
        "enabled": True,
        "search_queries": {"query-1": query},
    }
    notebook_path = notebook_dir / "示例主题__keyword-1.json"
    notebook_path.write_text(json.dumps(notebook, ensure_ascii=False), encoding="utf-8")
    page_dir = tmp_path / "pending_pages" / "keyword-1" / "query-1" / "openalex" / "backfill"
    page_dir.mkdir(parents=True)
    (page_dir / "page-1.json").write_text("{}", encoding="utf-8")

    report = inventory_notebooks(notebook_dir)

    row = report["notebooks"][0]
    assert report["unmapped"] == 0
    assert row["keyword_id"] == "keyword-1"
    assert row["queries"][0]["query_id"] == "query-1"
    state = row["providers"]["openalex"][0]
    assert state["generation"] == 3
    assert state["request_signature"] == "signature-1"
    assert state["cursor"] == "opaque-cursor"
    assert state["page_journal_count"] == 1


def _progressed_query(cursor: str, *, generation: int = 2, signature: str = "a" * 16,
                      exhausted: bool = False) -> dict:
    entry = _empty_query(
        "blowing snow", "en", "pytest", active=True,
        migration_at="2026-01-01T00:00:00Z",
    )
    backfill = entry["providers"]["openalex"]["backfill"]
    backfill.update({
        "cursor": cursor,
        "exhausted": exhausted,
        "pages_succeeded": 2,
        "pages_committed": 2,
        "items_returned_total": 4,
        "last_page_count": 2,
        "last_committed_page_id": f"last-{cursor}",
        "request_signature": signature,
        "generation": generation,
    })
    return entry


def _legacy_source(filename: str, keyword_id: str, query_id: str, entry: dict) -> LegacyQuery:
    return LegacyQuery(
        source_filename=filename,
        old_keyword_id=keyword_id,
        old_query_ids=(query_id,),
        query=entry["query"],
        language=entry["language"],
        entry=entry,
    )


def _merge_fixture(*, left_cursor: str = "opaque-z", right_cursor: str = "opaque-a",
                   page_records: dict | None = None, left_generation: int = 2,
                   right_generation: int = 2, left_signature: str = "a" * 16,
                   right_signature: str = "a" * 16, right_exhausted: bool = False):
    left = _progressed_query(left_cursor, generation=left_generation, signature=left_signature)
    right = _progressed_query(right_cursor, generation=right_generation, signature=right_signature,
                               exhausted=right_exhausted)
    qid = left["query_id"]
    left_source = _legacy_source("left.json", "legacy-left", "old-left-query", left)
    right_source = _legacy_source("right.json", "legacy-right", "old-right-query", right)
    target = {qid: left}
    proofs: list[dict] = []
    _merge_query(
        target,
        right,
        context="pytest",
        incoming_source=right_source,
        source_by_query={(qid, "openalex"): left_source},
        page_records=page_records if page_records is not None else {},
        cursor_merges=proofs,
    )
    return target[qid], proofs


def test_same_generation_journal_proves_forward_cursor_without_lexical_ordering():
    page_records = {
        "page-1": {
            "page_id": "page-1",
            "keyword_id": "legacy-left",
            "query_id": "old-left-query",
            "provider": "openalex",
            "lane": "backfill",
            "generation": 2,
            "request_signature": "a" * 16,
            "request_cursor": "opaque-z",
            "next_cursor": "opaque-a",
        },
    }
    merged, proofs = _merge_fixture(page_records=page_records)
    state = merged["providers"]["openalex"]["backfill"]
    assert state["cursor"] == "opaque-a"
    assert proofs[0]["proof"] == {
        "type": "page_journal_chain",
        "direction": "left_to_right",
        "journal_ids": ["page-1"],
    }


def test_same_generation_without_journal_proof_blocks():
    with pytest.raises(MigrationBlocked, match="unproven cursor divergence"):
        _merge_fixture()


def test_same_generation_journal_proves_backward_cursor_without_lexical_ordering():
    pages = {
        "page-1": {
            "page_id": "page-1",
            "keyword_id": "legacy-right",
            "query_id": "old-right-query",
            "provider": "openalex",
            "lane": "backfill",
            "generation": 2,
            "request_signature": "a" * 16,
            "request_cursor": "opaque-a",
            "next_cursor": "opaque-z",
        },
    }
    merged, proofs = _merge_fixture(
        left_cursor="opaque-z", right_cursor="opaque-a", page_records=pages,
    )
    assert merged["providers"]["openalex"]["backfill"]["cursor"] == "opaque-z"
    assert proofs[0]["proof"] == {
        "type": "page_journal_chain",
        "direction": "right_to_left",
        "journal_ids": ["page-1"],
    }


def test_pristine_and_progressed_duplicate_query_selects_progressed_state():
    left = _empty_query("blowing snow", "en", "pytest", active=True, migration_at="2026-01-01T00:00:00Z")
    right = _progressed_query("opaque-a")
    qid = left["query_id"]
    source = _legacy_source("right.json", "legacy-right", "old-right-query", right)
    target = {qid: left}
    _merge_query(target, right, context="pytest", incoming_source=source)
    assert target[qid]["providers"]["openalex"]["backfill"]["cursor"] == "opaque-a"


def test_progressed_and_pristine_duplicate_query_keeps_progressed_state():
    left = _progressed_query("opaque-a")
    right = _empty_query("blowing snow", "en", "pytest", active=True, migration_at="2026-01-01T00:00:00Z")
    qid = left["query_id"]
    source = _legacy_source("right.json", "legacy-right", "old-right-query", right)
    target = {qid: left}
    _merge_query(target, right, context="pytest", incoming_source=source)
    assert target[qid]["providers"]["openalex"]["backfill"]["cursor"] == "opaque-a"


def test_identical_duplicate_query_is_idempotent():
    left = _progressed_query("opaque-a")
    target = {left["query_id"]: left}
    _merge_query(target, left.copy(), context="pytest")
    assert target[left["query_id"]]["providers"]["openalex"]["backfill"]["cursor"] == "opaque-a"


@pytest.mark.parametrize(
    ("left_generation", "right_generation", "left_signature", "right_signature", "message"),
    [
        (2, 3, "a" * 16, "a" * 16, "generation mismatch"),
        (2, 2, "a" * 16, "b" * 16, "request signature mismatch"),
    ],
)
def test_duplicate_progress_conflicts_block(
    left_generation, right_generation, left_signature, right_signature, message,
):
    with pytest.raises(MigrationBlocked, match=message):
        _merge_fixture(
            left_generation=left_generation,
            right_generation=right_generation,
            left_signature=left_signature,
            right_signature=right_signature,
        )


def test_divergent_journal_successors_block():
    pages = {
        "page-a": {
            "page_id": "page-a", "keyword_id": "legacy-left", "query_id": "old-left-query",
            "provider": "openalex", "lane": "backfill", "generation": 2,
            "request_signature": "a" * 16, "request_cursor": "opaque-z", "next_cursor": "opaque-a",
        },
        "page-b": {
            "page_id": "page-b", "keyword_id": "legacy-right", "query_id": "old-right-query",
            "provider": "openalex", "lane": "backfill", "generation": 2,
            "request_signature": "a" * 16, "request_cursor": "opaque-z", "next_cursor": "opaque-b",
        },
    }
    with pytest.raises(MigrationBlocked, match="unproven cursor divergence"):
        _merge_fixture(page_records=pages)


def test_proven_merge_preserves_exhaustion_and_error_statistics():
    pages = {
        "page-1": {
            "page_id": "page-1", "keyword_id": "legacy-left", "query_id": "old-left-query",
            "provider": "openalex", "lane": "backfill", "generation": 2,
            "request_signature": "a" * 16, "request_cursor": "opaque-z", "next_cursor": "opaque-a",
        },
    }
    merged, _ = _merge_fixture(page_records=pages, right_exhausted=True)
    state = merged["providers"]["openalex"]["backfill"]
    assert state["cursor"] == "opaque-a"
    assert state["exhausted"] is True
    assert state["pages_committed"] == 2


def test_flat_provider_migration_preserves_generation_and_history():
    state = _translate_legacy_provider_state({
        "backfill_cursor": "opaque-z",
        "generation": 4,
        "generation_history": [{
            "generation": 3,
            "request_signature": "b" * 16,
            "closed_at": "2025-01-01T00:00:00Z",
            "reason": "reset",
        }],
        "request_signature": "a" * 16,
        "pages_committed": 1,
    }, context="pytest")
    assert state["backfill"]["generation"] == 4
    assert state["backfill"]["generation_history"][0]["generation"] == 3


def test_legacy_progress_binds_provider_state_to_page_signature():
    source = {
        "schema_version": "2.0",
        "keyword_id": "legacy-topic",
        "keyword": "风吹雪",
        "expansions": {
            "old-query": {
                "query": "blowing snow",
                "language": "en",
                "active": True,
                "providers": {
                    "openalex": {"backfill": {
                        "cursor": "opaque-cursor",
                        "pages_succeeded": 1,
                        "pages_committed": 1,
                        "items_returned_total": 1,
                        "request_signature": "c" * 16,
                        "generation": 1,
                    }},
                    "crossref": {},
                },
            },
        },
    }
    rows = _legacy_queries(
        source,
        filename="legacy.json",
        migration_at="2026-01-01T00:00:00Z",
        page_records={"page": {
            "page_id": "page",
            "keyword_id": "legacy-topic",
            "expansion_id": "old-query",
            "provider": "openalex",
            "lane": "backfill",
            "generation": 1,
            "request_signature": {"hash": "b" * 16},
        }},
    )
    assert rows[0].entry["providers"]["openalex"]["backfill"]["request_signature"] == "b" * 16
