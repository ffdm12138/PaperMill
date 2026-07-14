from __future__ import annotations

import json

import pytest

import src.discovery.notebook_v3_migration as migration
from src.discovery.notebook_v3_migration import (
    JournalConflict,
    migrate_notebooks_v3,
    rollback_migration,
    sha256_file,
)
from src.discovery.models import PaperCandidate
from src.discovery.page_journal import PageJournalStore, request_signature


def _roots(tmp_path):
    roots = {name: tmp_path / name for name in ("notebooks", "retired", "pages", "locks", "tx")}
    for path in roots.values():
        path.mkdir()
    old = roots["notebooks"] / "old.json"
    old.write_text(json.dumps({
        "schema_version": "2.0", "keyword_id": "legacy-topic", "keyword": "风吹雪",
        "expansions": {
            "zh": {"query": "风吹雪", "language": "zh", "active": True, "providers": {}},
            "en": {"query": "blowing snow", "language": "en", "active": True, "providers": {}},
        },
    }, ensure_ascii=False), encoding="utf-8")
    query = tmp_path / "queries.json"
    query.write_text(json.dumps({
        "schema_version": "1.0", "source": "pytest", "topics": [{
            "keyword_zh": "风吹雪", "enabled": True,
            "classification": {"guidance_zh": None, "aliases_zh": [], "exclusions_zh": []},
            "search_queries": [
                {"query": "风吹雪", "language": "zh"},
                {"query": "blowing snow", "language": "en"},
            ],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    mapping = tmp_path / "mapping.json"
    mapping.write_text(json.dumps({"schema_version": "1.0", "mappings": [{
        "source_notebook": old.name, "source_sha256": sha256_file(old),
        "keyword_zh": "风吹雪", "status": "confirmed",
    }]}, ensure_ascii=False), encoding="utf-8")
    common = dict(notebook_dir=roots["notebooks"], retired_dir=roots["retired"],
                  pending_pages_dir=roots["pages"], locks_dir=roots["locks"],
                  transaction_root=roots["tx"], query_manifest_path=query,
                  mapping_manifest_path=mapping)
    return common, old, mapping


def _add_multi_generation_pages(common, old, mapping):
    sig1 = request_signature(page_size=10)
    sig2 = request_signature(page_size=20)
    payload = json.loads(old.read_text(encoding="utf-8"))
    history = [{
        "generation": 1,
        "request_signature": sig1["hash"],
        "closed_at": "2026-01-01T00:00:00Z",
        "reason": "request signature changed",
        "cursor": "c1",
        "exhausted": False,
        "pages_succeeded": 1,
        "pages_committed": 1,
        "items_returned_total": 1,
        "last_committed_page_id": "historical-page",
    }]
    current = {
        "cursor": "d1",
        "exhausted": False,
        "pages_succeeded": 1,
        "pages_committed": 1,
        "items_returned_total": 1,
        "last_page_count": 1,
        "last_committed_page_id": "current-page",
        "request_signature": sig2["hash"],
        "generation": 2,
        "generation_history": history,
    }
    payload["expansions"]["en"]["providers"] = {
        "openalex": {"backfill": current},
        "crossref": {},
    }
    old.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    mapping_payload = json.loads(mapping.read_text(encoding="utf-8"))
    mapping_payload["mappings"][0]["source_sha256"] = sha256_file(old)
    mapping.write_text(json.dumps(mapping_payload, ensure_ascii=False), encoding="utf-8")

    journal = PageJournalStore(common["pending_pages_dir"])
    for page_id, generation, signature, next_cursor in (
        ("historical-page", 1, sig1, "c1"),
        ("current-page", 2, sig2, "d1"),
    ):
        page = journal.make_page(
            page_id=page_id,
            keyword_id="legacy-topic",
            keyword_zh="风吹雪",
            query_id="en",
            query="blowing snow",
            query_language="en",
            provider="openalex",
            lane="backfill",
            generation=generation,
            request_signature_value=signature,
            request_cursor=None,
            next_cursor=next_cursor,
            provider_exhausted=False,
            candidates=[PaperCandidate(title="T", doi=f"10.1234/{page_id}")],
            state="cursor_committed",
        )
        path = journal.page_path(
            keyword_id="legacy-topic", query_id="en", provider="openalex",
            lane="backfill", page_id=page_id,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
    return sig1, sig2


def test_apply_uses_durable_plan_and_is_idempotent(tmp_path):
    common, _, _ = _roots(tmp_path)
    planned = migrate_notebooks_v3(**common, write_plan=True, tx_id="transaction01")
    applied = migrate_notebooks_v3(**common, apply=True, tx_id="transaction01",
                                   expected_plan_sha256=planned["plan_sha256"])
    assert applied["status"] == "committed"
    repeated = migrate_notebooks_v3(**common, apply=True, tx_id="transaction01",
                                    expected_plan_sha256=planned["plan_sha256"])
    assert repeated["status"] == "already_committed"


def test_source_change_and_wrong_plan_hash_block_apply(tmp_path):
    common, old, _ = _roots(tmp_path)
    planned = migrate_notebooks_v3(**common, write_plan=True, tx_id="transaction02")
    with pytest.raises(JournalConflict, match="expected plan"):
        migrate_notebooks_v3(**common, apply=True, tx_id="transaction02",
                             expected_plan_sha256="0" * 64)
    old.write_text(old.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(JournalConflict, match="source changed"):
        migrate_notebooks_v3(**common, apply=True, tx_id="transaction02",
                             expected_plan_sha256=planned["plan_sha256"])


@pytest.mark.parametrize("hook, tx_id", [
    ("_write_backup", "transaction03"),
    ("_write_stage", "transaction04"),
    ("_install_outputs", "transaction05"),
    ("_archive_inputs", "transaction07"),
    ("_validate_installed", "transaction08"),
])
def test_crash_after_each_durable_phase_resumes(
    tmp_path, monkeypatch, hook: str, tx_id: str,
):
    common, _, _ = _roots(tmp_path)
    planned = migrate_notebooks_v3(**common, write_plan=True, tx_id=tx_id)
    original = getattr(migration, hook)

    def crash_after_phase(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError(f"crash after {hook}")

    monkeypatch.setattr(migration, hook, crash_after_phase)
    with pytest.raises(RuntimeError, match=f"crash after {hook}"):
        migrate_notebooks_v3(
            **common, apply=True, tx_id=tx_id,
            expected_plan_sha256=planned["plan_sha256"],
        )
    monkeypatch.setattr(migration, hook, original)
    resumed = migrate_notebooks_v3(
        **common, apply=True, resume=True, tx_id=tx_id,
        expected_plan_sha256=planned["plan_sha256"],
    )
    assert resumed["status"] == "committed"


def test_rollback_restores_original_notebook_after_commit(tmp_path):
    common, old, _ = _roots(tmp_path)
    planned = migrate_notebooks_v3(**common, write_plan=True, tx_id="transaction06")
    applied = migrate_notebooks_v3(
        **common, apply=True, tx_id="transaction06",
        expected_plan_sha256=planned["plan_sha256"],
    )
    assert applied["status"] == "committed"
    rolled_back = rollback_migration(**common, tx_id="transaction06")
    assert rolled_back["status"] == "rolled_back"
    assert old.is_file()
    assert json.loads(old.read_text(encoding="utf-8"))["schema_version"] == "2.0"
    assert rollback_migration(**common, tx_id="transaction06")["status"] == "already_rolled_back"


def test_multi_generation_pages_are_rewritten_and_rollback_restores_page_archive(tmp_path):
    common, old, mapping = _roots(tmp_path)
    _add_multi_generation_pages(common, old, mapping)
    original_notebook = old.read_bytes()
    original_pages = {
        path.relative_to(common["pending_pages_dir"]).as_posix(): path.read_bytes()
        for path in common["pending_pages_dir"].rglob("*.json")
    }
    planned = migrate_notebooks_v3(**common, write_plan=True, tx_id="transaction09")
    plan_path = tmp_path / "tx" / "discovery_keyword_v3" / "transaction09" / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert len(plan["pages"]) == 2
    assert {data["generation"] for data in plan["pages"].values()} == {1, 2}
    assert all(
        {"keyword_id", "keyword_zh", "query_id", "query", "query_language", "provider", "lane", "generation", "request_signature"}
        <= set(data)
        for data in plan["pages"].values()
    )
    applied = migrate_notebooks_v3(
        **common, apply=True, tx_id="transaction09",
        expected_plan_sha256=planned["plan_sha256"],
    )
    assert applied["status"] == "committed"
    assert len(list(common["pending_pages_dir"].rglob("*.json"))) == 2
    retired = common["retired_dir"] / "transaction09" / "pending_pages"
    assert len(list(retired.rglob("*.json"))) == 2

    rolled_back = rollback_migration(**common, tx_id="transaction09")
    assert rolled_back["status"] == "rolled_back"
    assert old.read_bytes() == original_notebook
    restored_pages = {
        path.relative_to(common["pending_pages_dir"]).as_posix(): path.read_bytes()
        for path in common["pending_pages_dir"].rglob("*.json")
    }
    assert restored_pages == original_pages
    assert not (common["retired_dir"] / "transaction09").exists()


def test_nonterminal_page_from_explicitly_retired_notebook_is_archived_and_restored(tmp_path):
    common, _, _ = _roots(tmp_path)
    retired_source = common["retired_dir"] / "english" / "legacy.json"
    retired_source.parent.mkdir(parents=True, exist_ok=True)
    retired_source.write_text(json.dumps({
        "schema_version": "2.0",
        "keyword_id": "retired-topic",
        "keyword": "legacy topic",
        "expansions": {
            "legacy-query": {
                "query": "legacy query",
                "language": "en",
                "active": True,
                "providers": {},
            },
        },
    }, ensure_ascii=False), encoding="utf-8")
    page = common["pending_pages_dir"] / "retired-topic" / "legacy-query" / "openalex" / "backfill" / "retired-page.json"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(json.dumps({
        "schema_version": "1.0",
        "page_id": "retired-page",
        "keyword_id": "retired-topic",
        "expansion_id": "legacy-query",
        "expanded_query": "legacy query",
        "provider": "openalex",
        "lane": "backfill",
        "state": "cursor_committed",
        "candidates": [{"candidate_id": "pending-candidate", "status": "pending"}],
    }, ensure_ascii=False), encoding="utf-8")
    original_page = page.read_bytes()

    planned = migrate_notebooks_v3(**common, write_plan=True, tx_id="transaction12")
    plan = json.loads((common["transaction_root"] / "discovery_keyword_v3" / "transaction12" / "plan.json").read_text(encoding="utf-8"))
    rel = page.relative_to(common["pending_pages_dir"]).as_posix()
    assert plan["pages"] == {}
    assert plan["identity"]["page_mapping"][rel] == "__archived_retired_source__"

    applied = migrate_notebooks_v3(
        **common, apply=True, tx_id="transaction12",
        expected_plan_sha256=planned["plan_sha256"],
    )
    assert applied["status"] == "committed"
    assert not page.exists()
    archived = common["retired_dir"] / "transaction12" / "pending_pages" / rel
    assert archived.read_bytes() == original_page

    rolled_back = rollback_migration(**common, tx_id="transaction12")
    assert rolled_back["status"] == "rolled_back"
    assert page.read_bytes() == original_page
    assert not (common["retired_dir"] / "transaction12").exists()


def test_fetched_page_matching_committed_notebook_state_is_promoted_in_plan(tmp_path):
    common, old, mapping = _roots(tmp_path)
    _add_multi_generation_pages(common, old, mapping)
    current = next(common["pending_pages_dir"].rglob("current-page.json"))
    current_data = json.loads(current.read_text(encoding="utf-8"))
    current_data["state"] = "fetched"
    current_data["cursor_committed_at"] = None
    current.write_text(json.dumps(current_data, ensure_ascii=False), encoding="utf-8")

    planned = migrate_notebooks_v3(**common, write_plan=True, tx_id="transaction13")
    plan = json.loads((common["transaction_root"] / "discovery_keyword_v3" / "transaction13" / "plan.json").read_text(encoding="utf-8"))
    promoted = next(data for data in plan["pages"].values() if data["page_id"] == "current-page")
    assert promoted["state"] == "cursor_committed"
    assert promoted["cursor_committed_at"] == promoted["fetched_at"]
    assert plan["page_state_reconciliations"] == [{
        "page_id": "current-page",
        "target_path": next(path for path in plan["pages"] if path.endswith("current-page.json")),
        "keyword_id": promoted["keyword_id"],
        "query_id": promoted["query_id"],
        "provider": "openalex",
        "generation": 2,
        "from_state": "fetched",
        "to_state": "cursor_committed",
        "proof": "last_committed_page_id_and_cursor_match",
    }]
    assert planned["plan_sha256"] == plan["plan_sha256"]


def test_query_and_mapping_manifest_change_blocks_apply(tmp_path):
    common, _, mapping = _roots(tmp_path)
    planned = migrate_notebooks_v3(**common, write_plan=True, tx_id="transaction10")
    query = tmp_path / "queries.json"
    query.write_text(query.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(JournalConflict, match="query manifest changed"):
        migrate_notebooks_v3(
            **common, apply=True, tx_id="transaction10",
            expected_plan_sha256=planned["plan_sha256"],
        )

    # Re-plan in a fresh transaction, then alter the mapping manifest.
    planned = migrate_notebooks_v3(**common, write_plan=True, tx_id="transaction11")
    mapping.write_text(mapping.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(JournalConflict, match="mapping manifest changed"):
        migrate_notebooks_v3(
            **common, apply=True, tx_id="transaction11",
            expected_plan_sha256=planned["plan_sha256"],
        )
