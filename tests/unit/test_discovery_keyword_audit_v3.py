from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import scripts.audit_discovery_keyword_index_sources as audit
from src.catalog_folders.registry import definition_hash
from src.discovery.keyword_notebook import (
    KeywordNotebookStore,
    keyword_id,
    notebook_filename,
    query_identity,
)
from src.discovery.models import PaperCandidate
from src.discovery.page_journal import PageJournalStore, request_signature


pytestmark = pytest.mark.unit


def _configure(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    notebooks = root / "notebooks"
    pending = root / "pending_pages"
    discovery = root / "discovery"
    exports = discovery / "exports"
    locks = root / "locks"
    catalog = root / "catalog"
    state = root / "catalog_state"
    for path in (notebooks, pending, exports, locks, catalog, state):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(audit, "DISCOVERY_KEYWORD_NOTEBOOK_DIR", notebooks)
    monkeypatch.setattr(audit, "DISCOVERY_PENDING_PAGES_DIR", pending)
    monkeypatch.setattr(audit, "DISCOVERY_DIR", discovery)
    monkeypatch.setattr(audit, "DISCOVERY_EXPORTS_DIR", exports)
    monkeypatch.setattr(audit, "DISCOVERY_LOCKS_DIR", locks)
    monkeypatch.setattr(audit, "CATALOG_FOLDER_ROOT", catalog)
    monkeypatch.setattr(audit, "CATALOG_STATE_ROOT", state)
    return notebooks


def _seed_notebook(root: Path, keyword_zh: str = "风吹雪", *, enabled: bool = True) -> dict:
    store = KeywordNotebookStore(root)
    store.ensure_notebook(keyword_zh)
    store.sync_search_queries(keyword_zh, add=[
        {"query": keyword_zh, "language": "zh", "source": "pytest"},
        {"query": "blowing snow", "language": "en", "source": "pytest"},
    ])
    if enabled:
        store.set_enabled(keyword_zh, True)
    return store.require_v3(keyword_zh)


def _write_registry(root: Path, notebook: dict) -> None:
    row = {
        "category_id": notebook["keyword_id"],
        "keyword_zh": notebook["keyword_zh"],
        "normalized_keyword_zh": notebook["normalized_keyword_zh"],
        "directory_name": notebook["keyword_zh"],
        "source_notebook": notebook_filename(notebook["keyword_zh"]),
        "guidance_zh": None,
        "aliases_zh": [],
        "exclusions_zh": [],
        "classification_enabled": True,
    }
    row["definition_sha256"] = definition_hash(row)
    (root / "category_registry.json").write_text(
        json.dumps({"schema_version": "1.0", "categories": [row]}, ensure_ascii=False),
        encoding="utf-8",
    )


def _audit_ready(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[dict, dict]:
    notebooks = _configure(monkeypatch, tmp_path)
    notebook = _seed_notebook(notebooks)
    _write_registry(tmp_path / "catalog_state", notebook)
    # Create the category directory so audit does not warn about a missing one.
    (tmp_path / "catalog" / notebook["keyword_zh"]).mkdir(parents=True, exist_ok=True)
    return audit.run_audit(), notebook


def test_valid_bilingual_notebook_passes_strict_audit(monkeypatch, tmp_path: Path):
    report, _ = _audit_ready(monkeypatch, tmp_path)
    assert report["notebook_schema_safe"] is True
    assert report["discovery_query_ready"] is True
    assert report["backfill_state_safe"] is True
    assert report["page_journal_safe"] is True
    assert report["receipt_provenance_safe"] is True
    assert report["migration_safe"] is True
    assert report["errors"] == []


def test_missing_chinese_query_fails_readiness(monkeypatch, tmp_path: Path):
    report, notebook = _audit_ready(monkeypatch, tmp_path)
    path = tmp_path / "notebooks" / notebook_filename(notebook["keyword_zh"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    for entry in payload["search_queries"].values():
        if entry["language"] == "zh":
            entry["active"] = False
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    report = audit.run_audit()
    assert any(row["kind"] == "readiness" for row in report["errors"])
    assert report["discovery_query_ready"] is False


@pytest.mark.parametrize("kind", ["keyword_id", "legacy", "query_id"])
def test_notebook_identity_and_legacy_shape_fail_closed(monkeypatch, tmp_path: Path, kind: str):
    _configure(monkeypatch, tmp_path)
    notebook = _seed_notebook(tmp_path / "notebooks")
    path = tmp_path / "notebooks" / notebook_filename(notebook["keyword_zh"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    if kind == "keyword_id":
        payload["keyword_id"] = "0" * 16
    elif kind == "legacy":
        payload["expansions"] = {}
    else:
        first = next(iter(payload["search_queries"].values()))
        first["query_id"] = "0" * 16
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    report = audit.run_audit()
    assert report["errors"]
    assert report["notebook_schema_safe"] is False


def test_missing_provider_generation_fails_schema_audit(monkeypatch, tmp_path: Path):
    _configure(monkeypatch, tmp_path)
    notebook = _seed_notebook(tmp_path / "notebooks")
    path = tmp_path / "notebooks" / notebook_filename(notebook["keyword_zh"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    query = next(iter(payload["search_queries"].values()))
    del query["providers"]["openalex"]["backfill"]["generation"]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    report = audit.run_audit()
    assert any(row["kind"] == "notebook_schema" for row in report["errors"])


def _write_page(root: Path, *, keyword_zh: str, keyword_id_value: str, query_id_value: str,
                query: str = "blowing snow", signature: dict | None = None,
                page_id: str = "page-1", generation: int = 1,
                request_cursor: str | None = None,
                next_cursor: str | None = "opaque-next",
                provider_exhausted: bool = False,
                state: str = "fetched") -> None:
    journal = PageJournalStore(root)
    value = journal.make_page(
        page_id=page_id,
        keyword_id=keyword_id_value,
        keyword_zh=keyword_zh,
        query_id=query_id_value,
        query=query,
        query_language="en",
        provider="openalex",
        lane="backfill",
        generation=generation,
        request_signature_value=signature or request_signature(page_size=10),
        request_cursor=request_cursor,
        next_cursor=next_cursor,
        provider_exhausted=provider_exhausted,
        candidates=[PaperCandidate(title="T", doi="10.1234/a")],
        state=state,
    )
    path = journal.page_path(
        keyword_id=keyword_id_value,
        query_id=query_id_value,
        provider="openalex",
        lane="backfill",
        page_id=page_id,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _multi_generation_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[dict, str, dict, dict]:
    notebooks = _configure(monkeypatch, tmp_path)
    notebook = _seed_notebook(notebooks)
    _write_registry(tmp_path / "catalog_state", notebook)
    qid = query_identity("en", "blowing snow")
    sig1 = request_signature(page_size=10)
    sig2 = request_signature(page_size=20)
    path = notebooks / notebook_filename(notebook["keyword_zh"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    backfill = payload["search_queries"][qid]["providers"]["openalex"]["backfill"]
    backfill.update({
        "generation": 2,
        "request_signature": sig2["hash"],
        "cursor": "d1",
        "pages_succeeded": 1,
        "pages_committed": 1,
        "items_returned_total": 1,
        "last_page_count": 1,
        "last_committed_page_id": "current-page",
        "generation_history": [{
            "generation": 1,
            "request_signature": sig1["hash"],
            "closed_at": "2026-01-01T00:00:00Z",
            "reason": "test restart",
            "cursor": "c1",
            "exhausted": False,
            "pages_succeeded": 1,
            "pages_committed": 1,
            "items_returned_total": 1,
            "last_committed_page_id": "historical-page",
        }],
    })
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _write_page(
        tmp_path / "pending_pages", keyword_zh=notebook["keyword_zh"],
        keyword_id_value=notebook["keyword_id"], query_id_value=qid,
        signature=sig1, page_id="historical-page", generation=1,
        next_cursor="c1", state="cursor_committed",
    )
    _write_page(
        tmp_path / "pending_pages", keyword_zh=notebook["keyword_zh"],
        keyword_id_value=notebook["keyword_id"], query_id_value=qid,
        signature=sig2, page_id="current-page", generation=2,
        next_cursor="d1", state="cursor_committed",
    )
    return payload, qid, sig1, sig2


def test_audit_accepts_two_independent_generations(monkeypatch, tmp_path: Path):
    _multi_generation_fixture(monkeypatch, tmp_path)
    report = audit.run_audit()
    assert report["errors"] == []
    assert report["summary"]["historical_generations"] == 1
    assert report["summary"]["current_generations"] == 1


def test_audit_checks_current_generation_against_current_state(monkeypatch, tmp_path: Path):
    payload, qid, _, _ = _multi_generation_fixture(monkeypatch, tmp_path)
    payload["search_queries"][qid]["providers"]["openalex"]["backfill"]["cursor"] = "wrong"
    path = tmp_path / "notebooks" / notebook_filename(payload["keyword_zh"])
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    report = audit.run_audit()
    assert any(row["kind"] == "backfill_state" and row.get("generation") == 2 for row in report["errors"])


def test_audit_checks_old_generation_against_history(monkeypatch, tmp_path: Path):
    payload, qid, _, _ = _multi_generation_fixture(monkeypatch, tmp_path)
    history = payload["search_queries"][qid]["providers"]["openalex"]["backfill"]["generation_history"]
    history[0]["last_committed_page_id"] = "missing-history-page"
    path = tmp_path / "notebooks" / notebook_filename(payload["keyword_zh"])
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    report = audit.run_audit()
    assert any(row["kind"] == "backfill_state" and row.get("generation") == 1 for row in report["errors"])


def test_audit_rejects_branch_inside_one_generation(monkeypatch, tmp_path: Path):
    _, qid, _, sig2 = _multi_generation_fixture(monkeypatch, tmp_path)
    notebook_path = next((tmp_path / "notebooks").glob("*.json"))
    notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
    _write_page(
        tmp_path / "pending_pages", keyword_zh=notebook["keyword_zh"],
        keyword_id_value=notebook["keyword_id"], query_id_value=qid,
        signature=sig2, page_id="current-branch", generation=2,
        next_cursor="d2", state="cursor_committed",
    )
    report = audit.run_audit()
    assert any("divergent" in row["message"] or "multiple" in row["message"] for row in report["errors"])


def test_audit_rejects_signature_mismatch_inside_generation(monkeypatch, tmp_path: Path):
    payload, qid, sig1, _ = _multi_generation_fixture(monkeypatch, tmp_path)
    _write_page(
        tmp_path / "pending_pages", keyword_zh=payload["keyword_zh"],
        keyword_id_value=payload["keyword_id"], query_id_value=qid,
        signature=sig1, page_id="wrong-signature", generation=2,
        next_cursor="bad", state="cursor_committed",
    )
    report = audit.run_audit()
    assert any(row["kind"] == "page_journal" and row.get("generation") == 2 for row in report["errors"])


def test_audit_rejects_generation_missing_from_history(monkeypatch, tmp_path: Path):
    payload, qid, _, sig2 = _multi_generation_fixture(monkeypatch, tmp_path)
    _write_page(
        tmp_path / "pending_pages", keyword_zh=payload["keyword_zh"],
        keyword_id_value=payload["keyword_id"], query_id_value=qid,
        signature=sig2, page_id="orphan-generation", generation=3,
        next_cursor="g3", state="cursor_committed",
    )
    report = audit.run_audit()
    assert any(row["kind"] == "generation" and row.get("generation") == 3 for row in report["errors"])


def test_audit_rejects_duplicate_request_cursor_within_generation(monkeypatch, tmp_path: Path):
    payload, qid, _, sig2 = _multi_generation_fixture(monkeypatch, tmp_path)
    _write_page(
        tmp_path / "pending_pages", keyword_zh=payload["keyword_zh"],
        keyword_id_value=payload["keyword_id"], query_id_value=qid,
        signature=sig2, page_id="duplicate-request", generation=2,
        next_cursor="d1", state="cursor_committed",
    )
    report = audit.run_audit()
    assert any("multiple page journals share one opaque request cursor" in row["message"] for row in report["errors"])


def test_orphan_page_and_signature_mismatch_fail_closed(monkeypatch, tmp_path: Path):
    _configure(monkeypatch, tmp_path)
    notebook = _seed_notebook(tmp_path / "notebooks")
    qid = query_identity("en", "blowing snow")
    _write_page(
        tmp_path / "pending_pages",
        keyword_zh=notebook["keyword_zh"],
        keyword_id_value=notebook["keyword_id"],
        query_id_value=qid,
    )
    report = audit.run_audit()
    assert any(row["kind"] == "page_journal" for row in report["errors"])

    other_qid = query_identity("en", "unmapped query")
    _write_page(
        tmp_path / "pending_pages",
        keyword_zh="雪崩动力学",
        keyword_id_value=keyword_id("雪崩动力学"),
        query_id_value=other_qid,
        query="unmapped query",
        page_id="orphan-page",
    )
    report = audit.run_audit()
    assert any("orphan" in row["message"] for row in report["errors"])


def test_english_query_is_not_a_catalog_category(monkeypatch, tmp_path: Path):
    report, notebook = _audit_ready(monkeypatch, tmp_path)
    assert report["errors"] == []
    assert "blowing snow" not in [row["keyword_zh"] for row in report["identities"]]
    assert notebook["keyword_zh"] == "风吹雪"


def test_disabled_draft_can_be_unready_without_audit_error(monkeypatch, tmp_path: Path):
    _configure(monkeypatch, tmp_path)
    KeywordNotebookStore(tmp_path / "notebooks").ensure_notebook("风吹雪")
    report = audit.run_audit()
    assert report["errors"] == []
    assert report["summary"]["disabled_drafts"] == 1
    assert report["warnings"]


def test_pristine_unbound_lane_is_summary_only(monkeypatch, tmp_path: Path):
    report, notebook = _audit_ready(monkeypatch, tmp_path)
    # Create category directory to suppress catalog_category warning
    (tmp_path / "catalog" / notebook["keyword_zh"]).mkdir(parents=True, exist_ok=True)
    report = audit.run_audit()
    assert report["errors"] == []
    # 2 queries (zh, en) × 2 providers (openalex, crossref) = 4 pristine lanes
    assert report["summary"]["pristine_unbound_lanes"] == 4


def _set_backfill(path: Path, query_str: str, provider: str, field_updates: dict) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    qid = query_identity("en", query_str)
    backfill = payload["search_queries"][qid]["providers"][provider]["backfill"]
    backfill.update(field_updates)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_cursor_without_signature_is_error(monkeypatch, tmp_path: Path):
    report, notebook = _audit_ready(monkeypatch, tmp_path)
    path = tmp_path / "notebooks" / notebook_filename(notebook["keyword_zh"])
    _set_backfill(path, "blowing snow", "openalex", {"cursor": "c1"})
    report = audit.run_audit()
    assert any(row["kind"] == "notebook_schema" for row in report["errors"])
    assert report["notebook_schema_safe"] is False


def test_pages_succeeded_without_signature_is_error(monkeypatch, tmp_path: Path):
    report, notebook = _audit_ready(monkeypatch, tmp_path)
    path = tmp_path / "notebooks" / notebook_filename(notebook["keyword_zh"])
    _set_backfill(path, "blowing snow", "openalex", {"pages_succeeded": 1})
    report = audit.run_audit()
    assert any(row["kind"] == "notebook_schema" for row in report["errors"])
    assert report["notebook_schema_safe"] is False


def test_pages_committed_without_signature_is_error(monkeypatch, tmp_path: Path):
    report, notebook = _audit_ready(monkeypatch, tmp_path)
    path = tmp_path / "notebooks" / notebook_filename(notebook["keyword_zh"])
    _set_backfill(path, "blowing snow", "openalex", {"pages_committed": 1})
    report = audit.run_audit()
    assert any(row["kind"] == "notebook_schema" for row in report["errors"])
    assert report["notebook_schema_safe"] is False


def test_items_returned_without_signature_is_error(monkeypatch, tmp_path: Path):
    report, notebook = _audit_ready(monkeypatch, tmp_path)
    path = tmp_path / "notebooks" / notebook_filename(notebook["keyword_zh"])
    _set_backfill(path, "blowing snow", "openalex", {"items_returned_total": 1})
    report = audit.run_audit()
    # The notebook fails schema validation because the shared strict-pristine
    # predicate rejects non-pristine unbound state at schema-check time.
    assert any(row["kind"] == "notebook_schema" for row in report["errors"])
    assert report["notebook_schema_safe"] is False


def test_exhausted_without_signature_is_error(monkeypatch, tmp_path: Path):
    report, notebook = _audit_ready(monkeypatch, tmp_path)
    path = tmp_path / "notebooks" / notebook_filename(notebook["keyword_zh"])
    _set_backfill(path, "blowing snow", "openalex", {"exhausted": True})
    report = audit.run_audit()
    assert any(row["kind"] == "notebook_schema" for row in report["errors"])
    assert report["notebook_schema_safe"] is False


def test_last_committed_page_without_signature_is_error(monkeypatch, tmp_path: Path):
    report, notebook = _audit_ready(monkeypatch, tmp_path)
    path = tmp_path / "notebooks" / notebook_filename(notebook["keyword_zh"])
    _set_backfill(path, "blowing snow", "openalex", {"last_committed_page_id": "some-page"})
    report = audit.run_audit()
    assert any(row["kind"] == "notebook_schema" for row in report["errors"])
    assert report["notebook_schema_safe"] is False


def test_terminal_failure_without_signature_is_error(monkeypatch, tmp_path: Path):
    report, notebook = _audit_ready(monkeypatch, tmp_path)
    path = tmp_path / "notebooks" / notebook_filename(notebook["keyword_zh"])
    _set_backfill(path, "blowing snow", "openalex", {"terminal_failure": True})
    report = audit.run_audit()
    assert any(row["kind"] == "notebook_schema" for row in report["errors"])
    assert report["notebook_schema_safe"] is False


def test_terminal_failure_timestamp_without_signature_is_error(monkeypatch, tmp_path: Path):
    report, notebook = _audit_ready(monkeypatch, tmp_path)
    path = tmp_path / "notebooks" / notebook_filename(notebook["keyword_zh"])
    _set_backfill(path, "blowing snow", "openalex", {"terminal_failure_at": "2026-01-01T00:00:00"})
    report = audit.run_audit()
    assert any(row["kind"] == "notebook_schema" for row in report["errors"])
    assert report["notebook_schema_safe"] is False


def test_last_error_without_signature_is_warning(monkeypatch, tmp_path: Path):
    report, notebook = _audit_ready(monkeypatch, tmp_path)
    path = tmp_path / "notebooks" / notebook_filename(notebook["keyword_zh"])
    _set_backfill(path, "blowing snow", "openalex", {"last_error": "something went wrong"})
    report = audit.run_audit()
    # Schema validator now rejects all non-pristine unbound states, so this
    # is an error (notebook_schema), not a warning.
    assert any(row["kind"] == "notebook_schema" for row in report["errors"])
    assert report["notebook_schema_safe"] is False


def test_pristine_unbound_lane_not_in_warnings(monkeypatch, tmp_path: Path):
    """A pristine unbound lane must not generate per-lane warnings."""
    report, _ = _audit_ready(monkeypatch, tmp_path)
    assert not any(row["kind"] == "generation" for row in report["warnings"])


def test_pristine_unbound_lane_count_is_exact(monkeypatch, tmp_path: Path):
    """The pristine count must exactly match never-activated lanes with no warnings."""
    report, _ = _audit_ready(monkeypatch, tmp_path)
    # 2 queries (zh + en) x 2 providers (openalex + crossref) = 4 pristine lanes
    assert report["summary"]["pristine_unbound_lanes"] == 4
    assert report["errors"] == []
    assert report["warnings"] == []


def test_page_journal_without_signature_is_error(monkeypatch, tmp_path: Path):
    """A page journal on disk without a notebook signature is durable progress, not pristine.

    The notebook state may look pristine (empty cursor, zero counters) but if a
    committed page journal already exists for the current generation, the lane
    has durable progress and must not be classified as pristine unbound.
    """
    report, notebook = _audit_ready(monkeypatch, tmp_path)
    qid = query_identity("en", "blowing snow")
    # Write a page journal for the current generation (default generation=1).
    # The notebook state has no request_signature.
    _write_page(
        tmp_path / "pending_pages",
        keyword_zh=notebook["keyword_zh"],
        keyword_id_value=notebook["keyword_id"],
        query_id_value=qid,
    )
    report = audit.run_audit()
    # Must raise a generation error (durable progress without signature).
    assert any(
        row["kind"] == "generation"
        and row.get("keyword_id") == notebook["keyword_id"]
        and row.get("query_id") == qid
        for row in report["errors"]
    )
    assert report["backfill_state_safe"] is False
    # The lane must NOT be counted as pristine.
    # Only 3 pristine lanes remain (the other 3 of 4 lanes are still pristine;
    # the openalex/en lane now has a page journal so it is no longer pristine).
    assert report["summary"]["pristine_unbound_lanes"] == 3


def test_audit_is_read_only(monkeypatch, tmp_path: Path):
    _configure(monkeypatch, tmp_path)
    notebook = _seed_notebook(tmp_path / "notebooks")
    _write_registry(tmp_path / "catalog_state", notebook)
    paths = [path for path in tmp_path.rglob("*") if path.is_file()]
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    audit.run_audit()
    after_paths = [path for path in tmp_path.rglob("*") if path.is_file()]
    after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in after_paths}
    assert before == after


def test_audit_is_read_only_with_page_journal(monkeypatch, tmp_path: Path):
    """Audit must not write notebooks, generate signatures, modify cursors, or delete page journals."""
    _configure(monkeypatch, tmp_path)
    notebook = _seed_notebook(tmp_path / "notebooks")
    _write_registry(tmp_path / "catalog_state", notebook)
    qid = query_identity("en", "blowing snow")
    sig = request_signature(page_size=10)
    # Set a valid signature so the page journal is not an error — we want to
    # verify the audit leaves everything untouched even on a clean pass.
    path = tmp_path / "notebooks" / notebook_filename(notebook["keyword_zh"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    backfill = payload["search_queries"][qid]["providers"]["openalex"]["backfill"]
    backfill["request_signature"] = sig["hash"]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _write_page(
        tmp_path / "pending_pages",
        keyword_zh=notebook["keyword_zh"],
        keyword_id_value=notebook["keyword_id"],
        query_id_value=qid,
        signature=sig,
        page_id="page-1",
        next_cursor="opaque-next",
        state="cursor_committed",
    )
    # Also bump the notebook counters so the page chain is consistent.
    path = tmp_path / "notebooks" / notebook_filename(notebook["keyword_zh"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    backfill = payload["search_queries"][qid]["providers"]["openalex"]["backfill"]
    backfill.update({
        "cursor": "opaque-next",
        "pages_succeeded": 1,
        "pages_committed": 1,
        "items_returned_total": 1,
        "last_committed_page_id": "page-1",
    })
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    paths = [path for path in tmp_path.rglob("*") if path.is_file()]
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    report = audit.run_audit()
    assert report["errors"] == []
    after_paths = [path for path in tmp_path.rglob("*") if path.is_file()]
    after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in after_paths}
    assert before == after
