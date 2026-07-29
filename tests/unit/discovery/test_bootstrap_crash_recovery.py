"""Bootstrap crash-window recovery (final-freeze probe 14).

Every window simulates a crash at one point of the
staging → manifest → rename → pointer sequence and asserts the next
``bootstrap_initial_workspace`` call resumes deterministically — reusing
existing sealed state, never generating a replacement manifest.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.discovery.workspace as wsp


pytestmark = pytest.mark.unit


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """Isolate every discovery workspace path under tmp_path."""
    generations = tmp_path / "generations"
    staging = generations / ".staging"
    migrations = tmp_path / "migrations"
    for d in (generations, staging, migrations):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(wsp, "DISCOVERY_GENERATIONS_DIR", generations)
    monkeypatch.setattr(wsp, "STAGING_DIR", staging)
    monkeypatch.setattr(wsp, "DISCOVERY_MIGRATIONS_DIR", migrations)
    monkeypatch.setattr(
        wsp, "ACTIVE_GENERATION_PATH", tmp_path / "active_generation.json"
    )
    monkeypatch.setattr(
        wsp, "DISCOVERY_MAINTENANCE_LOCK_PATH", migrations / ".maintenance.lock"
    )
    return SimpleNamespace(
        tmp_path=tmp_path,
        generations=generations,
        staging=staging,
        active_path=tmp_path / "active_generation.json",
    )


def _stage_with_manifest(gid: str) -> tuple[wsp.DiscoveryWorkspace, str]:
    """Simulate a completed staging+manifest attempt; return (ws, sha256)."""
    staging = wsp.create_staging_workspace(gid)
    manifest = wsp.build_workspace_manifest(
        gid, staging.root, migration_id=wsp.BOOTSTRAP_MIGRATION_ID
    )
    return staging, wsp.write_workspace_manifest(staging.root, manifest)


class TestBootstrapCrashWindows:
    def test_window1_crash_after_staging_creation(self, iso):
        wsp.create_staging_workspace("v4-crash1")  # crash before manifest
        ws, created = wsp.bootstrap_initial_workspace(generation_id="v4-crash1")
        assert created is True
        assert ws.generation_id == "v4-crash1"
        assert wsp.WorkspaceResolver().resolve_active().generation_id == "v4-crash1"

    def test_window2_crash_after_manifest_write(self, iso):
        staging, manifest_hash = _stage_with_manifest("v4-crash2")
        ws, created = wsp.bootstrap_initial_workspace(generation_id="v4-crash2")
        assert created is True
        pointer = json.loads(iso.active_path.read_text(encoding="utf-8"))
        # The pointer binds the ORIGINAL staged manifest bytes.
        assert pointer["workspace_manifest_sha256"] == manifest_hash

    def test_window3_crash_after_rename_before_pointer(self, iso):
        staging, manifest_hash = _stage_with_manifest("v4-crash3")
        target = iso.generations / "v4-crash3"
        os.rename(str(staging.root), str(target))  # crash before pointer write
        original_bytes = (target / "workspace.json").read_bytes()

        ws, created = wsp.bootstrap_initial_workspace(generation_id="v4-crash3")

        assert created is False  # recovered, not recreated
        assert ws.generation_id == "v4-crash3"
        pointer = json.loads(iso.active_path.read_text(encoding="utf-8"))
        assert pointer["workspace_manifest_sha256"] == manifest_hash
        # The sealed manifest was reused, never regenerated.
        assert (target / "workspace.json").read_bytes() == original_bytes
        assert not (iso.staging / "v4-crash3").exists()

    def test_window3_recovers_without_explicit_id(self, iso):
        staging, manifest_hash = _stage_with_manifest("v4-crash3b")
        os.rename(str(staging.root), str(iso.generations / "v4-crash3b"))
        ws, created = wsp.bootstrap_initial_workspace()
        assert created is False
        assert ws.generation_id == "v4-crash3b"
        pointer = json.loads(iso.active_path.read_text(encoding="utf-8"))
        assert pointer["workspace_manifest_sha256"] == manifest_hash

    def test_window4_crash_after_pointer_write(self, iso):
        first, created_first = wsp.bootstrap_initial_workspace(
            generation_id="v4-crash4"
        )
        assert created_first is True
        second, created_second = wsp.bootstrap_initial_workspace(
            generation_id="v4-crash4"
        )
        assert created_second is False
        assert second.generation_id == first.generation_id

    def test_window5_duplicate_same_generation_id_is_idempotent(self, iso):
        first, _ = wsp.bootstrap_initial_workspace(generation_id="v4-dup")
        second, created = wsp.bootstrap_initial_workspace(generation_id="v4-dup")
        assert created is False
        assert second.generation_id == first.generation_id

    def test_window6_multiple_unpointed_generations_fail_closed(self, iso):
        for gid in ("v4-amb1", "v4-amb2"):
            staging, _ = _stage_with_manifest(gid)
            os.rename(str(staging.root), str(iso.generations / gid))
        with pytest.raises(wsp.CommitReconciliationError):
            wsp.bootstrap_initial_workspace()

    def test_multiple_leftover_stagings_fail_closed(self, iso):
        wsp.create_staging_workspace("v4-s1")
        wsp.create_staging_workspace("v4-s2")
        with pytest.raises(wsp.CommitReconciliationError):
            wsp.bootstrap_initial_workspace()

    def test_damaged_unpointed_generation_fails_closed(self, iso):
        staging, _ = _stage_with_manifest("v4-damaged")
        target = iso.generations / "v4-damaged"
        os.rename(str(staging.root), str(target))
        # Damage the sealed closure after the "crash".
        (target / "keyword_notebooks" / "tampered.json").write_text(
            "{}", encoding="utf-8"
        )
        with pytest.raises(wsp.CommitReconciliationError):
            wsp.bootstrap_initial_workspace(generation_id="v4-damaged")
        # The damaged state was not stomped: still no pointer.
        assert not iso.active_path.exists()
