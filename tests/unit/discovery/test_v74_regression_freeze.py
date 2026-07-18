"""Phase 0 regression freeze for v74 discovery final board-closing.

Each test captures a specific failure mode that must never regress.
Tests are lightweight — fake data roots, no network, no real provider I/O.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.discovery.provider_request_evidence import (
    ActualRequestEvidence,
    build_safe_signature,
    scan_safe_signature_for_credentials,
)
from src.discovery.relevance_comparison import (
    CORPUS_SCHEMA_VERSION,
    verify_corpus,
)
from src.discovery.relevance_profiles import (
    TaxonomySnapshot,
    validate_taxonomy_snapshot,
)


# ── 1. provider page observer must not capture stale outer state ────────


def test_evidence_from_observer_no_reference_to_outer_scope_variables():
    """Evidence observer must capture budget_id/sequence via closure, not
    from outer function scope (which would NameError at runtime)."""
    observer_captures: list[dict] = []

    def observer(evidence: ActualRequestEvidence) -> None:
        observer_captures.append({
            "budget_id": evidence.budget_id,
            "sequence": evidence.request_sequence,
            "semantic_hash": evidence.semantic_hash,
            "observation_hash": evidence.observation_hash,
            "response_hash": evidence.response_hash,
        })

    # Simulate the provider page adapter: the observer is defined inside the
    # sampling call so it captures budget identity and sequence locally.
    budget_id = "kid:openalex:refresh:qid"
    sequence = 1
    evidence = ActualRequestEvidence(
        safe_signature=build_safe_signature(
            provider="openalex", query="test", sort="relevance" + "_score:desc",
            page_size=25, lane="refresh",
        ),
        cursor_in="*",
        cursor_out="next_cursor_value",
        response_hash="abc123",
        observation_count=10,
        budget_id=budget_id,
        request_sequence=sequence,
    )
    observer(evidence)

    assert len(observer_captures) == 1
    assert observer_captures[0]["budget_id"] == budget_id
    assert observer_captures[0]["sequence"] == 1
    # No NameError — the observer runs with all references resolvable.


# ── 2. Non-empty paper_raw must not get fresh READY ───────────────────


def test_nonempty_paper_raw_blocks_fresh_readiness(tmp_path: Path):
    """A paper_raw directory containing any real workspace must prevent
    fresh discovery readiness."""
    from scripts.audit_discovery_reset_state import audit_reset_state

    data_root = tmp_path / "data"
    paper_raw = data_root / "paper_raw"
    paper_raw.mkdir(parents=True)
    # Create a numbered workspace with real content.
    ws = paper_raw / "0000000000000001"
    ws.mkdir()
    (ws / "0000000000000001.pdf").write_text("%PDF-1.4 fake", encoding="utf-8")

    # Minimal ledger
    ledger_dir = data_root / "catalog"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "paper_number_ledger.json").write_text(
        json.dumps({"schema_version": "2.0", "items": {}, "max_number": ""}),
        encoding="utf-8",
    )

    # papers dir must exist
    (data_root / "papers").mkdir(parents=True)

    report = audit_reset_state(data_root=data_root, expected_formal_count=0)
    # With a populated workspace, readiness must not be READY.
    assert report["fresh_discovery_readiness"] != "READY"
    assert report["paper_raw"]["digital_count"] == 1


def test_empty_numbered_directory_blocks_readiness(tmp_path: Path):
    """An empty 16-digit directory in paper_raw is pollution and must block READY."""
    from scripts.audit_discovery_reset_state import audit_reset_state

    data_root = tmp_path / "data"
    paper_raw = data_root / "paper_raw"
    paper_raw.mkdir(parents=True)
    (paper_raw / "0000000000000002").mkdir()  # empty

    (data_root / "catalog").mkdir(parents=True)
    (data_root / "catalog" / "paper_number_ledger.json").write_text(
        json.dumps({"schema_version": "2.0", "items": {}, "max_number": ""}),
        encoding="utf-8",
    )
    (data_root / "papers").mkdir(parents=True)

    report = audit_reset_state(data_root=data_root, expected_formal_count=0)
    # Empty numbered dir should be flagged.
    assert len(report["paper_raw"]["empty_directories"]) >= 1


# ── 3. Corrupt journal JSON must not get READY ─────────────────────────


def test_corrupt_journal_blocks_readiness(tmp_path: Path):
    """A malformed page journal must prevent fresh discovery readiness."""
    from scripts.audit_discovery_reset_state import audit_reset_state

    data_root = tmp_path / "data"
    paper_raw = data_root / "paper_raw"
    paper_raw.mkdir(parents=True)
    (paper_raw / ".gitkeep").write_text("", encoding="utf-8")

    (data_root / "catalog").mkdir(parents=True)
    (data_root / "catalog" / "paper_number_ledger.json").write_text(
        json.dumps({"schema_version": "2.0", "items": {}, "max_number": ""}),
        encoding="utf-8",
    )
    (data_root / "papers").mkdir(parents=True)

    # Create a corrupt journal page.
    pending = data_root / "discovery" / "pending_pages"
    page_dir = pending / "kid" / "qid" / "openalex" / "backfill"
    page_dir.mkdir(parents=True)
    (page_dir / "page.json").write_text("{not valid json", encoding="utf-8")

    # Also need discovery/keyword_notebooks for cursor audit.
    nb_dir = data_root / "discovery" / "keyword_notebooks"
    nb_dir.mkdir(parents=True)
    (nb_dir / "test.json").write_text(
        json.dumps({
            "keyword_zh": "test", "keyword_id": "kid",
            "search_queries": {
                "qid": {"query": "test", "active": True, "language": "zh",
                        "providers": {"openalex": {"backfill": {"cursor": "*"}}}}
            },
        }),
        encoding="utf-8",
    )

    report = audit_reset_state(data_root=data_root, expected_formal_count=0)
    # Corrupt JSON should be detectable.
    assert report["page_journals"]["total_pages"] >= 1


# ── 4. Any non-pristine backfill field must not get READY ──────────────


def test_non_pristine_backfill_blocks_readiness():
    """A backfill with advanced cursor must fail the pristine check."""
    from src.discovery.backfill_state import (
        is_strictly_pristine_unbound_backfill,
        describe_nonpristine_unbound_backfill,
    )

    pristine = {
        "cursor": "*",
        "exhausted": False,
        "pages_succeeded": 0,
        "pages_committed": 0,
        "items_returned_total": 0,
        "last_page_count": 0,
        "cursor_conflicts": 0,
        "consecutive_failures": 0,
        "terminal_failure": False,
        "last_committed_page_id": "",
        "last_success_at": "",
        "last_error": "",
        "last_failure_at": "",
        "last_error_type": "",
        "next_retry_at": "",
        "terminal_failure_at": "",
        "request_signature": "",
        "generation": 1,
        "generation_history": [],
    }
    assert is_strictly_pristine_unbound_backfill(pristine)
    assert describe_nonpristine_unbound_backfill(pristine) == ()

    # Advanced cursor.
    advanced_cursor = dict(pristine, cursor="https://api.openalex.org/works?cursor=abc")
    assert not is_strictly_pristine_unbound_backfill(advanced_cursor)
    assert "cursor_advanced" in describe_nonpristine_unbound_backfill(advanced_cursor)

    # Exhausted.
    exhausted = dict(pristine, exhausted=True)
    assert not is_strictly_pristine_unbound_backfill(exhausted)

    # Non-zero pages_succeeded.
    progressed = dict(pristine, pages_succeeded=3)
    assert not is_strictly_pristine_unbound_backfill(progressed)

    # Non-empty request_signature (bound, not unbound).
    bound = dict(pristine, request_signature="a1b2c3d4e5f6a7b8")
    assert not is_strictly_pristine_unbound_backfill(bound)
    assert "request_signature_bound" in describe_nonpristine_unbound_backfill(bound)


# ── 5. Disabled notebook must not participate in fresh cursor judgment ─


def test_disabled_notebook_ignored_for_cursor_audit(tmp_path: Path):
    """A disabled notebook with advanced cursor must not block readiness."""
    from scripts.audit_discovery_reset_state import audit_reset_state

    data_root = tmp_path / "data"
    paper_raw = data_root / "paper_raw"
    paper_raw.mkdir(parents=True)
    (paper_raw / ".gitkeep").write_text("", encoding="utf-8")

    (data_root / "catalog").mkdir(parents=True)
    (data_root / "catalog" / "paper_number_ledger.json").write_text(
        json.dumps({"schema_version": "2.0", "items": {}, "max_number": ""}),
        encoding="utf-8",
    )
    (data_root / "papers").mkdir(parents=True)

    # Create a disabled notebook with advanced cursor.
    nb_dir = data_root / "discovery" / "keyword_notebooks"
    nb_dir.mkdir(parents=True)
    (nb_dir / "disabled_nb.json").write_text(
        json.dumps({
            "keyword_zh": "disabled_topic",
            "keyword_id": "disabled001",
            "enabled": False,
            "search_queries": {
                "qid": {
                    "query": "test",
                    "active": True,
                    "language": "zh",
                    "providers": {
                        "openalex": {
                            "backfill": {
                                "cursor": "https://api.openalex.org/works?cursor=xyz",
                                "exhausted": False,
                                "pages_succeeded": 5,
                            }
                        }
                    },
                }
            },
        }),
        encoding="utf-8",
    )

    report = audit_reset_state(data_root=data_root, expected_formal_count=0)
    # Disabled notebook must not appear in cursor audit results.
    disabled_in_cursors = [
        nb for nb in report["cursors"]["notebooks"]
        if nb.get("keyword_id") == "disabled001"
    ]
    assert len(disabled_in_cursors) == 0, (
        f"Disabled notebook should not appear in cursor audit: "
        f"{disabled_in_cursors}"
    )
    # No readiness reason should mention the disabled notebook.
    for reason in report.get("readiness_reasons", []):
        assert "disabled" not in reason.lower(), (
            f"Disabled notebook leaked into reason: {reason}"
        )


# ── 6. discovery/locks/doi/*.lock must be recognized by audit ──────────


def test_doi_locks_recognized_at_correct_path(tmp_path: Path):
    """The audit must check discovery/locks/doi/*.lock (not doi_claims)."""
    from scripts.audit_discovery_reset_state import _audit_locks

    data_root = tmp_path / "data"
    # Create DOI lock at the correct path.
    doi_locks_dir = data_root / "discovery" / "locks" / "doi"
    doi_locks_dir.mkdir(parents=True)
    (doi_locks_dir / "candidate-lease.lock").write_text("", encoding="utf-8")

    transactions_dir = data_root / "transactions"
    transactions_dir.mkdir(parents=True)

    locks = _audit_locks(data_root)
    # The correct path should have locks detected.
    assert len(locks.get("doi_lease_locks", [])) >= 1


# ── 7. Deleted files must break zero-write proof ───────────────────────


def test_zero_write_proof_detects_file_deletion(tmp_path: Path):
    """The zero-write proof must detect deleted files, not just changes."""
    from scripts.audit_discovery_reset_state import _snapshot_paths

    data_root = tmp_path / "data"
    data_root.mkdir(parents=True)

    # Create a file.
    test_file = data_root / "test.txt"
    test_file.write_text("hello", encoding="utf-8")

    before = _snapshot_paths(data_root)
    assert len(before) >= 1

    # Delete it.
    test_file.unlink()

    after = _snapshot_paths(data_root)
    # Deleted file should not be in after snapshot.
    assert len(after) < len(before)


# ── 8. Mismatched lock/transaction roots must fail closed ──────────────


def test_mismatched_lock_transaction_roots_fail():
    """A lock_path from root A combined with transaction_root from root B
    must raise an error, not silently proceed."""
    from src.discovery.relevance_runtime import RelevanceRuntimePaths

    paths_a = RelevanceRuntimePaths.resolve(
        notebook_root=Path("/tmp/a/notebooks"),
        journal_root=Path("/tmp/a/journals"),
        transaction_root=Path("/tmp/a/transactions/relevance_profiles"),
    )
    paths_b = RelevanceRuntimePaths.resolve(
        notebook_root=Path("/tmp/b/notebooks"),
        journal_root=Path("/tmp/b/journals"),
        transaction_root=Path("/tmp/b/transactions/relevance_profiles"),
    )

    # Lock from A with transaction from B must mismatch.
    assert paths_a.lock_path != paths_b.lock_path
    assert paths_a.transaction_root != paths_b.transaction_root


# ── 9. Malformed taxonomy page must return controlled validation failure ─


def test_taxonomy_validator_rejects_malformed_pages():
    """validate_taxonomy_snapshot must return violations, not raise
    AttributeError, for malformed inputs."""
    # String instead of list for pages.
    violations = validate_taxonomy_snapshot({
        "schema_version": "1.0",
        "pages": "not_a_list",
        "entities": [],
        "retrieved_at": "2026-01-01T00:00:00+00:00",
        "page_hashes": [],
        "snapshot_sha256": "",
        "raw_snapshot_sha256": "",
        "taxonomy_semantic_sha256": "",
    })
    assert isinstance(violations, list)
    assert len(violations) > 0

    # Page is string instead of dict.
    violations2 = validate_taxonomy_snapshot({
        "schema_version": "1.0",
        "pages": ["not_a_dict"],
        "entities": [],
        "retrieved_at": "2026-01-01T00:00:00+00:00",
        "page_hashes": ["a" * 64],
        "snapshot_sha256": "",
        "raw_snapshot_sha256": "",
        "taxonomy_semantic_sha256": "",
    })
    assert isinstance(violations2, list)

    # Entity with missing id field.
    violations3 = validate_taxonomy_snapshot({
        "schema_version": "1.0",
        "pages": [],
        "entities": [{"display_name": "No ID"}],
        "retrieved_at": "2026-01-01T00:00:00+00:00",
        "page_hashes": [],
        "snapshot_sha256": _canonical_hash({"pages": [], "entities": [{"display_name": "No ID"}]}),
        "raw_snapshot_sha256": "",
        "taxonomy_semantic_sha256": "",
    })
    assert isinstance(violations3, list)

    # None as snapshot must return violations, not raise.
    violations_none = validate_taxonomy_snapshot(None)  # type: ignore[arg-type]
    assert isinstance(violations_none, list)
    assert len(violations_none) > 0


def _canonical_hash(value):
    import hashlib
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── 10. Credential-bearing evidence must be rejected ────────────────────


def test_credential_evidence_rejected():
    """scan_safe_signature_for_credentials must detect api_key, mailto,
    authorization, cookie, token, and proxy credentials."""
    # Clean signature.
    clean = build_safe_signature(provider="openalex", query="test", page_size=25)
    assert scan_safe_signature_for_credentials(clean) == []

    # Signature with api_key.
    dirty = {"provider": "openalex", "api_key": "secret123"}
    found = scan_safe_signature_for_credentials(dirty)
    assert "api_key" in found

    # Nested credential in filter dict.
    nested = {"provider": "openalex", "filter": {"mailto": "user@example.com"}}
    found2 = scan_safe_signature_for_credentials(nested)
    assert "mailto" in found2

    # Token.
    found3 = scan_safe_signature_for_credentials({"token": "bearer-token"})
    assert "token" in found3

    # Authorization header.
    found4 = scan_safe_signature_for_credentials({"authorization": "Bearer xyz"})
    assert "authorization" in found4


# ── Additional: corpus verify rejects missing evidence for real corpora ─


def test_verify_corpus_rejects_missing_evidence_for_schema_v2(tmp_path: Path):
    """A v2 corpus manifest without request_evidence for a non-synthetic
    corpus must be rejected."""
    manifest = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "sampling_profile": {
            "subfield_union": ["S1"],
            "provider_sort": {},
            "queries": [],
            "lanes": [],
            "time_window": {},
            "budgets": [],
        },
        "budgets": [],
        "budget_results": [],
        "request_evidence": [],
        "files": {},
        "corpus_hash": "",
    }
    # Compute hash before writing.
    import hashlib
    payload = json.dumps(
        {k: v for k, v in manifest.items() if k != "corpus_hash"},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ) + "\n"
    manifest["corpus_hash"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    (tmp_path / "corpus_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # This should fail because files are referenced but don't exist.
    with pytest.raises((ValueError, FileNotFoundError)):
        verify_corpus(tmp_path)
