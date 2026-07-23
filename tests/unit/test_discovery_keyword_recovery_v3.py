from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from tests.helpers.relevance_profiles import bind_test_relevance_profile

from src.discovery.keyword_notebook import KeywordNotebookStore, keyword_id, query_identity
from src.discovery.models import PaperCandidate
from src.discovery.page_journal import PageJournalStore, request_signature
from scripts.recover_discovery_keyword_notebooks import (
    RecoveryApplyUnavailable,
    recover_notebooks,
)


pytestmark = pytest.mark.unit


def _setup(tmp_path: Path, *, generation: int = 1, signature: dict | None = None,
           next_cursor: str | None = "opaque-next") -> tuple[Path, Path, dict, dict]:
    notebooks = tmp_path / "notebooks"
    pages = tmp_path / "pages"
    locks = tmp_path / "locks"
    notebooks.mkdir()
    pages.mkdir()
    locks.mkdir()
    sig = signature or request_signature(page_size=10)
    store = KeywordNotebookStore(notebooks)
    store.ensure_notebook("风吹雪")
    store.sync_search_queries("风吹雪", add=[
        {"query": "风吹雪", "language": "zh"},
        {"query": "blowing snow", "language": "en"},
    ], pag_sig=sig["hash"])
    notebook = store.require_v3("风吹雪")
    qid = query_identity("en", "blowing snow")
    journal = PageJournalStore(pages)
    page = journal.make_synthetic_page(
        page_id="page-1",
        keyword_id=notebook["keyword_id"],
        keyword_zh="风吹雪",
        query_id=qid,
        query="blowing snow",
        query_language="en",
        provider="openalex",
        lane="backfill",
        generation=generation,
        request_signature_value=sig,
        request_cursor=None,
        next_cursor=next_cursor,
        provider_exhausted=False,
        candidates=[PaperCandidate(title="T", doi="10.1234/recovery")],
    )
    path = journal.page_path(
        keyword_id=notebook["keyword_id"], query_id=qid,
        provider="openalex", lane="backfill", page_id="page-1",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
    return notebooks, pages, notebook, page


def _recover(notebooks: Path, pages: Path, *, tmp_path: Path) -> dict:
    return recover_notebooks(
        notebook_dir=notebooks,
        pending_pages_dir=pages,
        locks_dir=tmp_path / "locks",
        catalog_root=tmp_path / "catalog",
        transaction_root=tmp_path / "transactions",
    )


def _setup_multiple_generations(
    tmp_path: Path,
    *,
    current_cursor: str = "*",
    current_pages: int = 0,
    current_last_page: str = "",
) -> tuple[Path, Path, dict, dict, dict]:
    notebooks = tmp_path / "notebooks"
    pages = tmp_path / "pages"
    locks = tmp_path / "locks"
    notebooks.mkdir()
    pages.mkdir()
    locks.mkdir()
    sig1 = request_signature(page_size=10)
    sig2 = request_signature(page_size=20)
    store = KeywordNotebookStore(notebooks)
    store.ensure_notebook("风吹雪")
    store.sync_search_queries(
        "风吹雪",
        add=[
            {"query": "风吹雪", "language": "zh"},
            {"query": "blowing snow", "language": "en"},
        ],
        pag_sig=sig2["hash"],
    )
    notebook_path = next(notebooks.glob("*.json"))
    notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
    qid = query_identity("en", "blowing snow")
    backfill = notebook["search_queries"][qid]["providers"]["openalex"]["backfill"]
    backfill.update({
        "generation": 2,
        "request_signature": sig2["hash"],
        "cursor": current_cursor,
        "pages_succeeded": current_pages,
        "pages_committed": current_pages,
        "items_returned_total": current_pages,
        "last_page_count": 1 if current_pages else 0,
        "last_committed_page_id": current_last_page,
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
    notebook_path.write_text(json.dumps(notebook, ensure_ascii=False), encoding="utf-8")
    journal = PageJournalStore(pages)
    for page_id, generation, signature, next_cursor in (
        ("historical-page", 1, sig1, "c1"),
        ("current-page", 2, sig2, "d1"),
    ):
        page = journal.make_synthetic_page(
            page_id=page_id,
            keyword_id=notebook["keyword_id"],
            keyword_zh="风吹雪",
            query_id=qid,
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
        )
        path = journal.page_path(
            keyword_id=notebook["keyword_id"], query_id=qid,
            provider="openalex", lane="backfill", page_id=page_id,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
    return notebooks, pages, notebook, sig1, sig2


def test_valid_journal_chain_produces_identity_bound_inspect_plan(tmp_path: Path):
    notebooks, pages, notebook, _ = _setup(tmp_path)
    report = _recover(notebooks, pages, tmp_path=tmp_path)
    assert report["inspect_only"] is True
    assert report["applied"] is False
    assert report["errors"] == []
    operation = report["plan"]["operations"][0]
    assert operation["keyword_id"] == notebook["keyword_id"]
    assert operation["query_id"] == query_identity("en", "blowing snow")
    assert operation["provider"] == "openalex"
    assert operation["generation"] == 1
    assert operation["action"] == "would_restore_current_backfill_state"
    assert operation["recoverable"] is True
    assert operation["source_journals"] == [
        f"{notebook['keyword_id']}/{operation['query_id']}/openalex/backfill/page-1.json"
    ]
    body = {key: value for key, value in report["plan"].items() if key != "plan_sha256"}
    expected = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert report["plan_sha256"] == expected
    assert report["plan"]["plan_sha256"] == expected


def test_inspect_is_read_only(tmp_path: Path):
    notebooks, pages, _, _ = _setup(tmp_path)
    tracked = [path for path in tmp_path.rglob("*") if path.is_file()]
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in tracked}
    report = _recover(notebooks, pages, tmp_path=tmp_path)
    after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in tracked}
    assert report["applied"] is False
    assert before == after


def test_apply_is_rejected_for_v3(tmp_path: Path):
    notebooks, pages, _, _ = _setup(tmp_path)
    with pytest.raises(RecoveryApplyUnavailable, match="inspect-only"):
        recover_notebooks(
            notebook_dir=notebooks,
            pending_pages_dir=pages,
            apply=True,
        )


@pytest.mark.parametrize("case", ["generation", "signature"])
def test_generation_or_signature_mismatch_blocks(tmp_path: Path, case: str):
    notebooks, pages, _, _ = _setup(tmp_path)
    path = next((pages).rglob("page-1.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    if case == "generation":
        payload["generation"] = 2
        payload["lane_key"]["generation"] = 2
    else:
        signature = request_signature(page_size=11)
        payload["request_signature"] = signature
        payload["lane_key"]["request_signature"] = signature["hash"]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    report = _recover(notebooks, pages, tmp_path=tmp_path)
    assert report["plan"]["operations"] == []
    assert report["errors"]
    assert any(item["kind"] in {"generation", "signature"} for item in report["errors"])


def test_cursor_divergence_fails_closed(tmp_path: Path):
    notebooks, pages, notebook, page = _setup(tmp_path)
    journal = PageJournalStore(pages)
    second = dict(page)
    second["page_id"] = "page-2"
    second["next_cursor"] = "opaque-other"
    second_path = journal.page_path(
        keyword_id=notebook["keyword_id"], query_id=page["query_id"],
        provider="openalex", lane="backfill", page_id="page-2",
    )
    second_path.write_text(json.dumps(second, ensure_ascii=False), encoding="utf-8")
    report = _recover(notebooks, pages, tmp_path=tmp_path)
    assert report["plan"]["operations"] == []
    assert any(item["kind"] == "cursor_divergence" for item in report["errors"])


def test_orphan_page_is_reported_without_touching_notebook(tmp_path: Path):
    notebooks, pages, _, page = _setup(tmp_path)
    orphan = dict(page)
    orphan["page_id"] = "orphan"
    orphan["keyword_zh"] = "雪崩动力学"
    orphan["keyword_id"] = keyword_id("雪崩动力学")
    orphan["lane_key"] = dict(orphan["lane_key"])
    orphan["lane_key"]["keyword_id"] = orphan["keyword_id"]
    orphan_path = pages / orphan["keyword_id"] / page["query_id"] / "openalex" / "backfill" / "orphan.json"
    orphan_path.parent.mkdir(parents=True, exist_ok=True)
    orphan_path.write_text(json.dumps(orphan, ensure_ascii=False), encoding="utf-8")
    report = _recover(notebooks, pages, tmp_path=tmp_path)
    assert any(item["kind"] == "orphan_page" for item in report["errors"])


def test_recovery_ignores_historical_generations_for_restore(tmp_path: Path):
    notebooks, pages, notebook, _, _ = _setup_multiple_generations(tmp_path)
    report = _recover(notebooks, pages, tmp_path=tmp_path)
    assert report["errors"] == []
    assert [item["generation"] for item in report["plan"]["operations"]] == [2]
    assert report["plan"]["operations"][0]["action"] == "would_restore_current_backfill_state"
    assert report["summary"]["historical_generations"] == 1
    assert any(item["kind"] == "historical_generation" and item["generation"] == 1 for item in report["warnings"])
    assert notebook["keyword_id"] == report["plan"]["operations"][0]["keyword_id"]


def test_recovery_uses_only_current_generation(tmp_path: Path):
    notebooks, pages, _, _, _ = _setup_multiple_generations(tmp_path)
    report = _recover(notebooks, pages, tmp_path=tmp_path)
    assert all(item["generation"] == 2 for item in report["plan"]["operations"])
    assert not any(item["kind"] == "cursor_divergence" and item.get("generation") == 1 for item in report["errors"])


def test_recovery_reports_valid_history_without_false_positive(tmp_path: Path):
    notebooks, pages, _, _, _ = _setup_multiple_generations(
        tmp_path, current_cursor="d1", current_pages=1, current_last_page="current-page",
    )
    report = _recover(notebooks, pages, tmp_path=tmp_path)
    assert report["errors"] == []
    assert report["plan"]["operations"] == []
    assert report["summary"]["recovery_operations"] == 0
    assert report["summary"]["historical_generation_warnings"] == 1


def test_recovery_blocks_current_generation_branch(tmp_path: Path):
    notebooks, pages, notebook, _, sig2 = _setup_multiple_generations(tmp_path)
    qid = query_identity("en", "blowing snow")
    journal = PageJournalStore(pages)
    branch = journal.make_synthetic_page(
        page_id="current-branch",
        keyword_id=notebook["keyword_id"], keyword_zh="风吹雪",
        query_id=qid, query="blowing snow", query_language="en",
        provider="openalex", lane="backfill", generation=2,
        request_signature_value=sig2, request_cursor=None,
        next_cursor="d2", provider_exhausted=False,
        candidates=[PaperCandidate(title="T", doi="10.1234/current-branch")],
    )
    path = journal.page_path(
        keyword_id=notebook["keyword_id"], query_id=qid,
        provider="openalex", lane="backfill", page_id="current-branch",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(branch, ensure_ascii=False), encoding="utf-8")
    report = _recover(notebooks, pages, tmp_path=tmp_path)
    assert report["plan"]["operations"] == []
    assert any(item["kind"] == "cursor_divergence" and item.get("generation") == 2 for item in report["errors"])


def test_recovery_blocks_signature_mismatch(tmp_path: Path):
    notebooks, pages, notebook, sig1, _ = _setup_multiple_generations(tmp_path)
    qid = query_identity("en", "blowing snow")
    journal = PageJournalStore(pages)
    bad = journal.make_synthetic_page(
        page_id="bad-signature",
        keyword_id=notebook["keyword_id"], keyword_zh="风吹雪",
        query_id=qid, query="blowing snow", query_language="en",
        provider="openalex", lane="backfill", generation=2,
        request_signature_value=sig1, request_cursor=None,
        next_cursor="bad", provider_exhausted=False,
        candidates=[PaperCandidate(title="T", doi="10.1234/bad-signature")],
    )
    path = journal.page_path(
        keyword_id=notebook["keyword_id"], query_id=qid,
        provider="openalex", lane="backfill", page_id="bad-signature",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    report = _recover(notebooks, pages, tmp_path=tmp_path)
    assert report["plan"]["operations"] == []
    assert any(item["kind"] == "signature" and item.get("generation") == 2 for item in report["errors"])


def test_recovery_does_not_treat_generation_restart_as_cursor_divergence(tmp_path: Path):
    notebooks, pages, _, _, _ = _setup_multiple_generations(tmp_path)
    report = _recover(notebooks, pages, tmp_path=tmp_path)
    assert report["errors"] == []
    assert not any(item["kind"] == "cursor_divergence" for item in report["errors"])


def test_recovery_rejects_items_returned_without_signature(tmp_path: Path):
    """Recovery rejects a backfill with items_returned > 0 but no request_signature."""
    notebooks = tmp_path / "notebooks"
    pages = tmp_path / "pages"
    locks = tmp_path / "locks"
    notebooks.mkdir()
    pages.mkdir()
    locks.mkdir()
    store = KeywordNotebookStore(notebooks)
    store.ensure_notebook("风吹雪")
    store.sync_search_queries("风吹雪", add=[
        {"query": "风吹雪", "language": "zh", "source": "pytest"},
        {"query": "blowing snow", "language": "en", "source": "pytest"},
    ])
    bind_test_relevance_profile(store, "风吹雪")
    store.set_enabled("风吹雪", True)
    # Corrupt notebook: set items_returned_total without signature
    from src.discovery.keyword_notebook import notebook_filename, query_identity
    qid = query_identity("en", "blowing snow")
    path = notebooks / notebook_filename("风吹雪")
    payload = json.loads(path.read_text(encoding="utf-8"))
    bf = payload["search_queries"][qid]["providers"]["openalex"]["backfill"]
    bf["items_returned_total"] = 1
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    report = _recover(notebooks, pages, tmp_path=tmp_path)
    assert report["errors"], "Recovery should report errors for non-pristine unbound"
    assert report["plan"]["operations"] == []


def test_recovery_rejects_invalid_generation(tmp_path: Path):
    """Recovery rejects a backfill with generation = 0 (invalid range)."""
    notebooks = tmp_path / "notebooks"
    pages = tmp_path / "pages"
    locks = tmp_path / "locks"
    notebooks.mkdir()
    pages.mkdir()
    locks.mkdir()
    store = KeywordNotebookStore(notebooks)
    store.ensure_notebook("风吹雪")
    store.sync_search_queries("风吹雪", add=[
        {"query": "风吹雪", "language": "zh", "source": "pytest"},
        {"query": "blowing snow", "language": "en", "source": "pytest"},
    ])
    bind_test_relevance_profile(store, "风吹雪")
    store.set_enabled("风吹雪", True)
    from src.discovery.keyword_notebook import notebook_filename, query_identity
    qid = query_identity("en", "blowing snow")
    path = notebooks / notebook_filename("风吹雪")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["search_queries"][qid]["providers"]["openalex"]["backfill"]["generation"] = 0
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    report = _recover(notebooks, pages, tmp_path=tmp_path)
    assert report["errors"], "Recovery should reject generation=0"


def test_recovery_rejects_unknown_backfill_field(tmp_path: Path):
    """Recovery rejects a backfill with an unknown field name."""
    notebooks = tmp_path / "notebooks"
    pages = tmp_path / "pages"
    locks = tmp_path / "locks"
    notebooks.mkdir()
    pages.mkdir()
    locks.mkdir()
    store = KeywordNotebookStore(notebooks)
    store.ensure_notebook("风吹雪")
    store.sync_search_queries("风吹雪", add=[
        {"query": "风吹雪", "language": "zh", "source": "pytest"},
        {"query": "blowing snow", "language": "en", "source": "pytest"},
    ])
    bind_test_relevance_profile(store, "风吹雪")
    store.set_enabled("风吹雪", True)
    from src.discovery.keyword_notebook import notebook_filename, query_identity
    qid = query_identity("en", "blowing snow")
    path = notebooks / notebook_filename("风吹雪")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["search_queries"][qid]["providers"]["openalex"]["backfill"]["some_unknown_field"] = True
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    report = _recover(notebooks, pages, tmp_path=tmp_path)
    assert report["errors"], "Recovery should reject unknown backfill fields"
