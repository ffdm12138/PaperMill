"""Final-freeze negative probes for the Discovery v4 sealing contract.

Each probe in this module encodes one item of the frozen P0/P1 audit
checklist.  They are written against the TARGET behaviour: every probe
fails against the pre-freeze code (fail-open parsers, dead stores,
flattened runtime errors, symlink-tolerant resolver) and must be turned
green by the production fix — never by editing the probe expectations.

Probe inventory (frozen):
 1  wrong pointer schema ("3.0") rejected
 2  missing pointer schema_version rejected
 3  integer generation_id rejected (no type coercion)
 4  generation_id "." / ".." rejected (pointer and workspace)
 5  non-ISO activated_at rejected
 6  negative manifest count rejected
 7  bool manifest count rejected (bool is not int)
 8  empty / malformed SHA-256 fields rejected
 9  missing manifest required field rejected
 10 dead v4 stores (lane state / journal index / report) removed
 11 runtime error taxonomy exists and maps missing -> NotInitialized
 12 corrupt pointer/manifest maps to Corrupt (never NotInitialized)
 13 symlinked required subdirectory rejected
(14 bootstrap crash-window recovery and 15 writer/maintenance mutual
exclusion live in their own phase test modules.)
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from src.discovery.contracts.manifest import (
    ActiveGenerationPointerV4,
    DiscoveryWorkspaceManifestV4,
)

_H = "a" * 64
_TS = "2026-01-01T00:00:00+00:00"
_TS2 = "2026-01-01T00:00:01+00:00"


def _valid_pointer() -> dict:
    return {
        "schema_version": "4.0",
        "generation_id": "gen-abc123",
        "workspace_manifest_sha256": _H,
        "activated_at": _TS,
        "migration_id": "mig-1",
    }


def _valid_manifest() -> dict:
    return {
        "schema_version": "4.0",
        "generation_id": "gen-abc123",
        "migration_id": "mig-1",
        "created_at": _TS,
        "completed_at": _TS2,
        "notebook_count": 0,
        "query_count": 0,
        "page_journal_count": 0,
        "notebook_set_hash": _H,
        "page_journal_set_hash": _H,
        "relevance_profile_hash": _H,
        "store_schema_versions": {"notebooks": "4.0", "page_journals": "4.0"},
        "workspace_tree_sha256": _H,
        "migration_inventory_sha256": _H,
    }


# ── Probe 1: wrong pointer schema ────────────────────────────────────────


def test_probe_01_pointer_wrong_schema_rejected() -> None:
    data = _valid_pointer()
    data["schema_version"] = "3.0"
    with pytest.raises((ValueError, TypeError)):
        ActiveGenerationPointerV4.from_dict_strict(data)


# ── Probe 2: missing pointer schema_version ──────────────────────────────


def test_probe_02_pointer_missing_schema_rejected() -> None:
    data = _valid_pointer()
    del data["schema_version"]
    with pytest.raises((ValueError, TypeError)):
        ActiveGenerationPointerV4.from_dict_strict(data)


# ── Probe 3: integer generation_id (no coercion) ─────────────────────────


def test_probe_03_pointer_integer_generation_id_rejected() -> None:
    data = _valid_pointer()
    data["generation_id"] = 12345
    with pytest.raises((ValueError, TypeError)):
        ActiveGenerationPointerV4.from_dict_strict(data)


# ── Probe 4: "." / ".." generation ids ───────────────────────────────────


@pytest.mark.parametrize("bad_id", [".", "..", " ", "gen/evil", "CON"])
def test_probe_04_pointer_forbidden_generation_id_rejected(bad_id: str) -> None:
    data = _valid_pointer()
    data["generation_id"] = bad_id
    with pytest.raises((ValueError, TypeError)):
        ActiveGenerationPointerV4.from_dict_strict(data)


@pytest.mark.parametrize("bad_id", [".", ".."])
def test_probe_04b_workspace_forbidden_generation_id_rejected(
    bad_id: str,
) -> None:
    from src.discovery.workspace import DiscoveryWorkspace

    with pytest.raises(ValueError):
        DiscoveryWorkspace.from_generation_id(bad_id)


# ── Probe 5: non-ISO activated_at ────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_ts",
    [
        "2026-13-99T99:99:99+99:99",  # contains T and + but is not ISO
        "T+",
        "2026-01-01T00:00:00",  # naive (no timezone)
    ],
)
def test_probe_05_pointer_non_iso_time_rejected(bad_ts: str) -> None:
    data = _valid_pointer()
    data["activated_at"] = bad_ts
    with pytest.raises((ValueError, TypeError)):
        ActiveGenerationPointerV4.from_dict_strict(data)


# ── Probe 6: negative manifest count ─────────────────────────────────────


def test_probe_06_manifest_negative_count_rejected() -> None:
    data = _valid_manifest()
    data["notebook_count"] = -99
    with pytest.raises((ValueError, TypeError)):
        DiscoveryWorkspaceManifestV4.from_dict_strict(data)


# ── Probe 7: bool manifest count ─────────────────────────────────────────


def test_probe_07_manifest_bool_count_rejected() -> None:
    data = _valid_manifest()
    data["notebook_count"] = True
    with pytest.raises((ValueError, TypeError)):
        DiscoveryWorkspaceManifestV4.from_dict_strict(data)


# ── Probe 8: empty / malformed SHA-256 ───────────────────────────────────


@pytest.mark.parametrize("bad_hash", ["", "not-a-hash", "A" * 64, "a" * 63])
def test_probe_08_manifest_bad_hash_rejected(bad_hash: str) -> None:
    data = _valid_manifest()
    data["workspace_tree_sha256"] = bad_hash
    with pytest.raises((ValueError, TypeError)):
        DiscoveryWorkspaceManifestV4.from_dict_strict(data)


# ── Probe 9: missing manifest required field ─────────────────────────────


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "generation_id",
        "migration_id",
        "created_at",
        "completed_at",
        "notebook_count",
        "workspace_tree_sha256",
        "store_schema_versions",
    ],
)
def test_probe_09_manifest_missing_field_rejected(field: str) -> None:
    data = _valid_manifest()
    del data[field]
    with pytest.raises((ValueError, TypeError)):
        DiscoveryWorkspaceManifestV4.from_dict_strict(data)


# ── Probe 10: dead v4 stores removed ─────────────────────────────────────


def test_probe_10_dead_store_modules_removed() -> None:
    src_discovery = Path(__file__).resolve().parents[2] / "src" / "discovery"
    for rel in (
        "stores/lane_state_store.py",
        "stores/journal_index.py",
        "stores/report_store.py",
        "contracts/lane_state.py",
    ):
        assert not (src_discovery / rel).exists(), f"dead store remains: {rel}"


def test_probe_10b_bundle_has_only_live_stores() -> None:
    import dataclasses

    from src.discovery.stores import bundle as bundle_mod

    fields = {f.name for f in dataclasses.fields(bundle_mod.DiscoveryStoreBundleV4)}
    assert fields == {"notebooks", "pages"}, f"unexpected bundle stores: {fields}"


# ── Probe 11/12: runtime error taxonomy ──────────────────────────────────


def test_probe_11_runtime_error_taxonomy_exists() -> None:
    rc = importlib.import_module("src.discovery.runtime_context")
    for name in (
        "DiscoveryRuntimeNotInitialized",
        "DiscoveryRuntimeCorrupt",
        "DiscoveryRuntimeIncomplete",
    ):
        assert hasattr(rc, name), f"missing typed runtime error: {name}"


def test_probe_11b_missing_active_maps_to_not_initialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rc = importlib.import_module("src.discovery.runtime_context")
    from src.discovery.workspace import ActiveGenerationMissingError

    class _Resolver:
        def resolve_active(self, **_kwargs):
            raise ActiveGenerationMissingError("no pointer")

    monkeypatch.setattr(rc, "WorkspaceResolver", lambda *a, **k: _Resolver())
    with pytest.raises(rc.DiscoveryRuntimeNotInitialized):
        rc.resolve_active_runtime()


@pytest.mark.parametrize(
    "cause_name,expected_name",
    [
        ("ActiveGenerationCorruptError", "DiscoveryRuntimeCorrupt"),
        ("WorkspaceManifestMissingError", "DiscoveryRuntimeCorrupt"),
        ("WorkspaceManifestMismatchError", "DiscoveryRuntimeCorrupt"),
        ("WorkspaceIncompleteError", "DiscoveryRuntimeIncomplete"),
    ],
)
def test_probe_12_corrupt_states_never_map_to_not_initialized(
    monkeypatch: pytest.MonkeyPatch, cause_name: str, expected_name: str
) -> None:
    rc = importlib.import_module("src.discovery.runtime_context")
    import src.discovery.workspace as ws_mod

    cause = getattr(ws_mod, cause_name)("broken")

    class _Resolver:
        def resolve_active(self, **_kwargs):
            raise cause

    monkeypatch.setattr(rc, "WorkspaceResolver", lambda *a, **k: _Resolver())
    expected = getattr(rc, expected_name)
    assert not issubclass(expected, rc.DiscoveryRuntimeNotInitialized)
    with pytest.raises(expected):
        rc.resolve_active_runtime()


# ── Probe 13: symlinked required subdirectory rejected ───────────────────


def test_probe_13_symlinked_subdir_rejected(tmp_path: Path) -> None:
    from src.discovery.workspace import (
        WorkspaceIncompleteError,
        WorkspaceManifestMismatchError,
        WorkspaceResolver,
    )

    root = tmp_path / "gen-sym"
    outside = tmp_path / "outside"
    outside.mkdir()
    for d in (
        "keyword_notebooks",
        "lane_states",
        "page_journals",
        "indexes",
        "exports",
        "reports",
        "locks",
    ):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "keyword_notebooks").rmdir()
    try:
        (root / "keyword_notebooks").symlink_to(
            outside, target_is_directory=True
        )
    except OSError:
        pytest.skip("symlink creation not permitted on this host")

    manifest = _valid_manifest()
    manifest["generation_id"] = root.name
    (root / "workspace.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    with pytest.raises(
        (WorkspaceIncompleteError, WorkspaceManifestMismatchError, ValueError)
    ):
        WorkspaceResolver.resolve_explicit_workspace(root)
