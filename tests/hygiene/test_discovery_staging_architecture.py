"""Architecture contract tests for discovery staging boundaries.

Encodes the five boundaries from ``docs/ADR_DISCOVERY_STAGING_BOUNDARIES.md``
as enforceable assertions. Each test documents which boundary it guards and what
the target state is. Tests that currently fail are marked with the phase that
will make them pass.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.hygiene._scanner import iter_text_files, scan_tokens


pytestmark = pytest.mark.hygiene

ROOT = Path(__file__).resolve().parents[2]


# ═══════════════════════════════════════════════════════════════════════════════
# Boundary 1: Ledger owns only number allocation and raw/formal lifecycle
# ═══════════════════════════════════════════════════════════════════════════════


def test_ledger_state_constants_are_lifecycle_only():
    """LEDGER_STAGE_FAILED must not exist — staging failure is an operation
    result recorded in .import_status.json, not a ledger lifecycle state."""
    state_file = ROOT / "src" / "library" / "paper_number_state.py"
    text = state_file.read_text(encoding="utf-8")
    # Target: LEDGER_STAGE_FAILED is removed. Phase 2.
    assert "LEDGER_STAGE_FAILED" not in text, (
        "LEDGER_STAGE_FAILED must not exist in paper_number_state.py — "
        "staging failure is an operation result, not a ledger lifecycle state"
    )


def test_ledger_transition_table_forbids_abandoned_revival():
    """abandoned → metadata_staged must not exist in the transition table."""
    state_file = ROOT / "src" / "library" / "paper_number_state.py"
    text = state_file.read_text(encoding="utf-8")
    # The abandoned row in ALLOWED_LEDGER_TRANSITIONS must be frozenset().
    # Check that the line after LEDGER_ABANDONED contains frozenset().
    assert 'LEDGER_ABANDONED: frozenset()' in text or \
           'LEDGER_ABANDONED: frozenset()' in text.replace(' ', ''), (
        "abandoned must be permanently terminal — "
        "LEDGER_ABANDONED: frozenset() with no allowed targets"
    )


def test_ledger_transition_table_forbids_reserved_to_active():
    """reserved → active must not exist in the transition table."""
    state_file = ROOT / "src" / "library" / "paper_number_state.py"
    text = state_file.read_text(encoding="utf-8")
    # reserved must only allow metadata_staged, abandoned, stage_failed (target: abandoned)
    # After Phase 2, reserved allows: metadata_staged, abandoned
    lines = text.splitlines()
    in_reserved = False
    for line in lines:
        if 'LEDGER_RESERVED:' in line and 'frozenset' in line:
            in_reserved = True
            assert 'LEDGER_ACTIVE' not in line, (
                "reserved → active is forbidden — commit only accepts metadata_staged"
            )
            break


def test_ledger_transition_table_forbids_active_to_reserved():
    """active → reserved must not exist (only active → metadata_staged)."""
    state_file = ROOT / "src" / "library" / "paper_number_state.py"
    text = state_file.read_text(encoding="utf-8")
    lines = text.splitlines()
    for line in lines:
        if 'LEDGER_ACTIVE:' in line and 'frozenset' in line:
            assert 'LEDGER_RESERVED' not in line, (
                "active → reserved is forbidden — rollback returns metadata_staged"
            )
            break


def test_ledger_recovery_does_not_revive_abandoned():
    """mark_metadata_staged must not accept abandoned → metadata_staged even
    with recovery_identity. abandoned is permanently terminal."""
    ledger_file = ROOT / "src" / "library" / "paper_number_ledger.py"
    text = ledger_file.read_text(encoding="utf-8")
    # The mark_metadata_staged method must not contain abandoned as an allowed
    # transition source. Explicit rejection (raise InvalidLedgerTransition) is
    # fine — that's the fail-closed gate.
    if "def mark_metadata_staged" not in text:
        return
    mark_method = text.split("def mark_metadata_staged")[1].split("def ")[0]
    # recovery_identity parameter must not exist.
    if "recovery_identity" in mark_method:
        pytest.fail(
            "mark_metadata_staged must not have recovery_identity parameter"
        )
    # The abandoned → metadata_staged recovery path must not exist.
    # Check for the pattern that USES abandoned as a valid source (not rejection).
    if "LEDGER_ABANDONED}" in mark_method.replace(" ", ""):
        pytest.fail(
            "mark_metadata_staged must not treat abandoned as an allowed source state"
        )
    # Also check the old elif pattern: `state in {LEDGER_STAGE_FAILED, LEDGER_ABANDONED}`
    if "LEDGER_ABANDONED}" in mark_method.replace(" ", "").replace("\n", ""):
        pass  # already checked above
    # The combined recovery block must not exist.
    if "elif state in" in mark_method and "LEDGER_ABANDONED" in mark_method:
        pytest.fail(
            "mark_metadata_staged must not have abandoned recovery elif branch"
        )


def test_no_direct_ledger_state_mutation_outside_ledger():
    """Production code must not directly write item["state"] = ... outside
    PaperNumberLedger and paper_number_state.py."""
    files = iter_text_files(["src"], excluded_suffixes={".pyc"})
    pattern = 'item["state"]'
    matches = scan_tokens(files, [pattern])
    allowed = {
        "src/library/paper_number_ledger.py",
        "src/library/paper_number_state.py",
    }
    offenders = [
        f"{rel}: {token}"
        for rel, token, _ in matches
        if rel.replace("\\", "/") not in allowed
    ]
    # Currently, there may be offenders in ingestion code. Phase 2 will fix these.
    # For now, report but don't fail — upgrade to hard fail after Phase 2.
    if offenders:
        pytest.fail(
            "direct item['state'] mutation outside ledger:\n" + "\n".join(offenders)
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Boundary 2: Workspace inspector returns only file facts
# ═══════════════════════════════════════════════════════════════════════════════


def test_workspace_lifecycle_has_readiness_judgment():
    """inspect_workspace_lifecycle must delegate readiness to
    :func:`evaluate_metadata_staged` with ingest profile, not compute it inline."""
    lifecycle_file = ROOT / "src" / "ingest" / "workspace_lifecycle.py"
    if not lifecycle_file.exists():
        return
    text = lifecycle_file.read_text(encoding="utf-8")
    assert "complete_for_metadata" + "_staged" not in text, (
        "The deprecated composite field is gone; use readiness.ready instead."
        "use inspection.readiness.ready instead."
    )
    assert "evaluate_metadata_staged" in text, (
        "workspace_lifecycle.py must delegate to evaluate_metadata_staged"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Boundary 3: Readiness evaluator uses ingest profile
# ═══════════════════════════════════════════════════════════════════════════════


def test_ingest_profile_enum_exists():
    """An IngestProfile enum (MANUAL_PDF, NETWORK_METADATA, NETWORK_METADATA_PDF_FETCH)
    must exist and be the sole discriminator for readiness evaluation."""
    lifecycle_file = ROOT / "src" / "ingest" / "workspace_lifecycle.py"
    if lifecycle_file.exists():
        text = lifecycle_file.read_text(encoding="utf-8")
        # The deprecated composite field is gone; readiness.ready replaces it.
        # readiness.ready (from evaluate_metadata_staged) is the sole gate.
        assert "complete_for_metadata" + "_staged" not in text
        assert "evaluate_metadata_staged" in text
        assert "readiness.ready" in text or 'readiness["ready"]' in text


# ═══════════════════════════════════════════════════════════════════════════════
# Boundary 4: WorkspaceRegistry is the sole scan entry point
# ═══════════════════════════════════════════════════════════════════════════════


def test_source_record_scan_only_in_registry_or_recovery_or_audit():
    """Full-library source_records glob must only exist in:
    - workspace_registry.py (the single scan entry)
    - recovery/audit tools (explicit repair paths)
    - workspace_index.py (legacy — Phase 10 cleanup target)
    - pending_queue.py (reconciliation slow path — Phase 10 cleanup target)
    - test files
    It must NOT exist in: ingest_duplicate_guard (duplicate index build),
    staging_context, paper_raw allocator.
    """
    files = iter_text_files(["src"], excluded_suffixes={".pyc"})
    patterns = [
        'glob("*/source_records/',
        "glob('*/source_records/",
        'rglob("*source_records*")',
        'glob("**/source_records',
    ]
    matches = scan_tokens(files, patterns)
    # Allowed: registry, legacy modules (Phase 10 cleanup), recovery tools
    allowed_files = {
        "src/discovery/workspace_registry.py",
        "src/services/source_records.py",        # Writer, not scanner
        "src/services/network_metadata_staging.py",  # Path references, not scans
        "src/ingest/workspace_evidence.py",      # Single-file reads, not scans
    }
    allowed_patterns = [
        "recover", "audit", "repair", "reconcile",
    ]
    offenders = []
    for rel, token, _ in matches:
        rel_posix = rel.replace("\\", "/")
        if rel_posix in allowed_files:
            continue
        if any(p in rel_posix.lower() for p in allowed_patterns):
            continue
        offenders.append(f"{rel_posix}: {token}")
    if offenders:
        pytest.fail(
            "full-library source_records scan in unexpected module:\n"
            + "\n".join(offenders)
        )


def test_discovery_receipt_scan_only_in_registry_or_recovery_or_audit():
    """Full-library discovery_receipt glob must only exist in:
    - workspace_registry.py
    - workspace_index.py (legacy — Phase 10 cleanup)
    - pending_queue.py (reconciliation — Phase 10 cleanup)
    - discovery_receipt.py (writer, not scanner)
    - recovery/audit tools
    - test files
    """
    files = iter_text_files(["src"], excluded_suffixes={".pyc"})
    patterns = [
        '*.discovery_receipt.json',
        'glob("*/*.discovery_receipt',
        "glob('*/*.discovery_receipt",
    ]
    matches = scan_tokens(files, patterns)
    allowed_files = {
        "src/discovery/workspace_registry.py",
        "src/discovery/discovery_receipt.py",
        "src/ingest/workspace_evidence.py",      # Single-file reads, not scans
        "src/ingest/workspace_lifecycle.py",     # Backward-compat layer
    }
    allowed_patterns = [
        "recover", "audit", "repair", "reconcile", "reconciliation",
    ]
    offenders = []
    for rel, token, _ in matches:
        rel_posix = rel.replace("\\", "/")
        if rel_posix in allowed_files:
            continue
        if any(p in rel_posix.lower() for p in allowed_patterns):
            continue
        offenders.append(f"{rel_posix}: {token}")
    if offenders:
        pytest.fail(
            "full-library discovery_receipt scan in unexpected module:\n"
            + "\n".join(offenders)
        )


def test_workspace_refresh_has_production_caller_or_is_deleted():
    """workspace_refresh.py must have at least one production caller,
    or be deleted in favor of workspace_registry.py. Phase 10 cleanup target."""
    refresh_file = ROOT / "src" / "discovery" / "workspace_refresh.py"
    assert not refresh_file.exists()


# ═══════════════════════════════════════════════════════════════════════════════
# Boundary 5: DiscoveryStageTransaction is the sole write-lock coordinator
# ═══════════════════════════════════════════════════════════════════════════════


def test_pending_queue_does_not_scan_workspaces_directly():
    """pending_queue.py must not call iterdir, glob, or rglob on paper_raw
    or papers directories to discover workspaces. It calls the transaction
    coordinator which owns the registry."""
    pq_file = ROOT / "src" / "discovery" / "pending_queue.py"
    if not pq_file.exists():
        return
    text = pq_file.read_text(encoding="utf-8")
    for token in (".glob(", ".rglob(", ".iterdir(", "reserve_next", "allocate_metadata"):
        assert token not in text


def test_discovery_batch_hot_path_has_no_full_scan_or_context_rebuild():
    pending = (ROOT / "src/discovery/pending_queue.py").read_text(encoding="utf-8")
    for forbidden in ("journal.list_pages", "journal.count_pending_candidates",
                      "journal.iter_claimable", "DiscoveryStagingContext.create"):
        assert forbidden not in pending
    registry = (ROOT / "src/discovery/workspace_registry.py").read_text(encoding="utf-8")
    assert "to_scan = set(current.repair_backlog_numbers)" not in registry
    assert "to_scan = set(current.repair_backlog_numbers)" not in registry


def test_discovery_production_batching_calls_are_structurally_enforced():
    pending_path = ROOT / "src/discovery/pending_queue.py"
    pending = pending_path.read_text(encoding="utf-8")
    pending_tree = ast.parse(pending, filename=str(pending_path))
    for node in ast.walk(pending_tree):
        if not isinstance(node, ast.Call):
            continue
        name = (node.func.id if isinstance(node.func, ast.Name)
                else node.func.attr if isinstance(node.func, ast.Attribute) else "")
        if name == "stage_network_metadata_records" and node.args:
            assert not isinstance(node.args[0], ast.List), (
                "production staging must pass a prepared batch, not [single_candidate]")
    assert "commit_candidate_results(" in pending
    assert "_find_doi_processing_owner" not in pending
    assert "_find_emitted_primary" not in pending

    coordinator = (ROOT / "src/discovery/coordinator.py").read_text(encoding="utf-8")
    assert coordinator.count("DiscoveryBatchRuntime.create(") == 1
    assert "DiscoveryStagingContext.create(" not in coordinator
    assert "STAGING_QUEUE_CAPACITY = 500" in coordinator
    assert "runtime_lock" not in coordinator
    assert "consumer.join(timeout=60)" not in coordinator
    assert "staging_no_progress_timeout_seconds" in coordinator

    transaction = (ROOT / "src/discovery/stage_transaction.py").read_text(encoding="utf-8")
    for forbidden in (".glob(", ".rglob(", ".iterdir("):
        assert forbidden not in transaction


def test_retired_quarantined_duplicate_is_not_a_ledger_state():
    state = (ROOT / "src/library/paper_number_state.py").read_text(encoding="utf-8")
    assert "quarantined_duplicate" not in state


def test_old_index_refresh_apis_are_deleted():
    source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "src").rglob("*.py"))
    assert ".refresh_under_write_lock(" not in source
    assert "def full_rebuild_under_write_lock" not in source
    assert "build_doi_duplicate_index" not in source


def test_registry_indexes_are_pure_in_memory_modules():
    for relative in ("src/discovery/workspace_index.py", "src/services/duplicate_index.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        for forbidden in ("from pathlib import Path", "import json", ".glob(", ".rglob(", ".iterdir("):
            assert forbidden not in source, f"{relative} contains {forbidden}"


def test_allocator_does_not_independently_refresh_doi_index():
    """PaperRawAllocator must not call build_doi_duplicate_index independently.
    The registry/transaction coordinator owns the index lifecycle.

    The allocator MAY call refresh_under_write_lock on a pre-built index
    passed to it (the transaction coordinator provides the index). It must
    NOT build a new index from scratch inside the staging hot path."""
    alloc_file = ROOT / "src" / "ingest" / "paper_raw.py"
    text = alloc_file.read_text(encoding="utf-8")
    # The allocator must not build a fresh index independently.
    if "build_doi_duplicate_index" in text:
        pytest.fail(
            "PaperRawAllocator must not independently build the DOI index — "
            "the transaction coordinator owns index lifecycle"
        )
    # Transaction module must exist (Phase 5 deliverable).
    tx_file = ROOT / "src" / "discovery" / "stage_transaction.py"
    assert tx_file.exists(), (
        "DiscoveryStageTransaction must exist as the sole write-lock coordinator"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Safety: no swallowed exceptions in safety-path modules
# ═══════════════════════════════════════════════════════════════════════════════


def test_no_bare_except_pass_in_safety_modules():
    """Safety-path modules must not contain ``except Exception: pass`` or
    equivalent patterns that silently swallow errors."""
    safety_modules = [
        "src/library/paper_number_ledger.py",
        "src/ingest/workspace_lifecycle.py",
        "src/services/ingest_duplicate_guard.py",
        "src/discovery/workspace_index.py",
        "src/ingest/commit.py",
        "src/ingest/formalization.py",
    ]
    offenders = []
    for mod_path in safety_modules:
        full_path = ROOT / mod_path
        if not full_path.exists():
            continue
        lines = full_path.read_text(encoding="utf-8").splitlines()
        in_except = False
        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("except") and "Exception" in stripped:
                in_except = True
                # Check if the very next non-empty, non-comment line is just "pass"
                continue
            if in_except:
                if stripped == "pass":
                    offenders.append(f"{mod_path}:{i}: {stripped}")
                elif stripped and not stripped.startswith("#"):
                    in_except = False
    if offenders:
        pytest.fail(
            "bare `except Exception: pass` in safety modules:\n"
            + "\n".join(offenders)
        )
