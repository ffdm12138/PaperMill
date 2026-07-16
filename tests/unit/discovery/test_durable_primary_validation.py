import json
from pathlib import Path

import pytest

from src.discovery.keyword_notebook import keyword_id, query_identity
from src.discovery.models import PaperCandidate
from src.discovery.page_journal import INITIAL_CURSOR, PageJournalStore, request_signature
from src.discovery.pending_queue import drain_pending_candidates, export_candidate_once, inspect_emitted_primary_export


pytestmark = pytest.mark.unit
KEYWORD_ZH = "测试关键词"
KEYWORD_ID = keyword_id(KEYWORD_ZH)
QUERY_ID = query_identity("zh", KEYWORD_ZH)
PROFILE_HASH = "test-active-profile"


def _write_page(store: PageJournalStore, page_id: str, doi: str = "10.1234/export") -> Path:
    page = store.make_page(
        page_id=page_id,
        keyword_id=KEYWORD_ID,
        keyword_zh=KEYWORD_ZH,
        query_id=QUERY_ID,
        query=KEYWORD_ZH,
        query_language="zh",
        provider="openalex",
        lane="refresh",
        request_signature_value=request_signature(page_size=10),
        request_cursor=INITIAL_CURSOR,
        next_cursor=None,
        provider_exhausted=True,
        candidates=[PaperCandidate(title=f"T {page_id}", doi=doi)],
        state="cursor_committed", relevance_profile_hash=PROFILE_HASH,
    )
    page["candidates"][0]["relevance"]["state"] = "passed"
    return store.write_page(page)


def _drain(tmp_path: Path, store: PageJournalStore):
    return drain_pending_candidates(
        journal=store,
        keyword_ids=[KEYWORD_ID],
        candidate_budget=10,
        stage_to_paper_raw=False,
        apply=False,
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        locks_dir=tmp_path / "locks",
        exports_dir=tmp_path / "exports",
        worker_id="worker",
        active_profile_hashes={KEYWORD_ID: PROFILE_HASH},
    )


def test_bare_export_id_is_not_durable_primary(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    primary = _write_page(store, "p1")
    secondary = _write_page(store, "p2")
    cid = store.read(primary)["candidates"][0]["candidate_id"]
    assert store.claim_candidate(primary, candidate_id_value=cid, worker_id="owner", lease_seconds=60).claimed
    store.commit_candidate(
        primary,
        candidate_id_value=cid,
        worker_id="owner",
        new_status="emitted",
        updates={"export_id": "bare-only"},
    )

    report = _drain(tmp_path, store)

    assert report.duplicate_observation == 0
    assert report.retryable_failures == 1
    item = store.read(secondary)["candidates"][0]
    assert item["status"] == "failed_retryable"
    assert item["last_deferred_reason"] == "doi_primary_validation_failed"


def test_complete_export_artifact_is_durable_primary(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    primary = _write_page(store, "p1")
    secondary = _write_page(store, "p2")
    data = store.read(primary)
    item = data["candidates"][0]
    item["page_id"] = data["page_id"]
    item["keyword_id"] = data["keyword_id"]
    item["provider"] = data["provider"]
    export = export_candidate_once(tmp_path / "exports", item)
    cid = item["candidate_id"]
    assert store.claim_candidate(primary, candidate_id_value=cid, worker_id="owner", lease_seconds=60).claimed
    store.commit_candidate(
        primary,
        candidate_id_value=cid,
        worker_id="owner",
        new_status="emitted",
        updates={
            "export_id": export["export_id"],
            "export_path": export["export_path"],
            "manifest_path": export["manifest_path"],
        },
    )

    report = _drain(tmp_path, store)

    assert report.duplicate_observation == 1
    assert store.read(secondary)["candidates"][0]["status"] == "duplicate_observation"


def test_legacy_manifest_without_artifact_hash_is_not_durable(tmp_path: Path):
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    export_path = export_dir / "legacy.jsonl"
    export_path.write_text(json.dumps({"doi": "10.1234/legacy"}) + "\n", encoding="utf-8")
    manifest_path = export_dir / "legacy.manifest.json"
    manifest_path.write_text(
        json.dumps({
            "schema_version": "1.0",
            "export_id": "legacy-id",
            "candidate_id": "candidate-a",
            "jsonl_path": export_path.as_posix(),
        }),
        encoding="utf-8",
    )
    durable, reason = inspect_emitted_primary_export(
        {
            "status": "emitted",
            "candidate_id": "candidate-a",
            "export_id": "legacy-id",
            "manifest_path": manifest_path.as_posix(),
        },
        "10.1234/legacy",
        exports_dir=export_dir,
    )

    assert not durable
    assert reason


@pytest.mark.parametrize(
    "manifest_update",
    [
        {"candidate_id": "other"},
        {"export_id": "other"},
        {"normalized_doi": "10.1234/other"},
    ],
)
def test_export_manifest_identity_mismatch_is_not_durable(tmp_path: Path, manifest_update: dict):
    record = {
        "candidate_id": "candidate-a",
        "page_id": "p1",
        "keyword_id": "kw",
        "provider": "openalex",
        "candidate": {"doi": "10.1234/mismatch", "title": "Mismatch"},
    }
    export = export_candidate_once(tmp_path / "exports", record)
    manifest_path = Path(export["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(manifest_update)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    durable, reason = inspect_emitted_primary_export(
        {
            "status": "emitted",
            "candidate_id": "candidate-a",
            "export_id": export["export_id"],
            "manifest_path": export["manifest_path"],
            "export_path": export["export_path"],
        },
        "10.1234/mismatch",
        exports_dir=tmp_path / "exports",
    )

    assert not durable
    assert reason
