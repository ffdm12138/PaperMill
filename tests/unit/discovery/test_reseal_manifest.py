"""Active-generation manifest reseal (operator recovery for the strict contract).

A generation sealed by the pre-freeze lenient writer carries empty set
hashes, ``{}`` store schema versions, and retired fields; the strict
resolver rejects it as corrupt.  ``reseal_active_generation_manifest``
recomputes real values from content and rebinds the pointer.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.discovery.workspace as wsp
from src.discovery.contracts.manifest import STORE_SCHEMA_VERSIONS_V4


pytestmark = pytest.mark.unit


@pytest.fixture
def iso(tmp_path, monkeypatch):
    generations = tmp_path / "generations"
    migrations = tmp_path / "migrations"
    generations.mkdir(parents=True)
    migrations.mkdir(parents=True)
    monkeypatch.setattr(wsp, "DISCOVERY_GENERATIONS_DIR", generations)
    monkeypatch.setattr(wsp, "STAGING_DIR", generations / ".staging")
    monkeypatch.setattr(wsp, "DISCOVERY_MIGRATIONS_DIR", migrations)
    monkeypatch.setattr(
        wsp, "ACTIVE_GENERATION_PATH", tmp_path / "active_generation.json"
    )
    monkeypatch.setattr(
        wsp, "DISCOVERY_MAINTENANCE_LOCK_PATH", migrations / ".maintenance.lock"
    )
    return SimpleNamespace(tmp_path=tmp_path, generations=generations)


def _seed_lenient_generation(iso, gid: str = "v4-lenient") -> Path:
    """Create a generation sealed under the OLD lenient contract."""
    gen_root = iso.generations / gid
    for sub in wsp.KNOWN_SUBDIRS:
        (gen_root / sub).mkdir(parents=True, exist_ok=True)
    (gen_root / "keyword_notebooks" / "nb__aaaa1111.json").write_text(
        json.dumps({"search_queries": {"q1": {"zh": "q1"}, "q2": {"en": "q2"}}}),
        encoding="utf-8",
    )
    old_manifest = {
        "schema_version": "4.0",
        "generation_id": gid,
        "migration_id": "bootstrap-v4-init",
        "created_at": "2026-07-23T08:08:58+00:00",
        "completed_at": "2026-07-23T08:08:58+00:00",
        "notebook_count": 0,
        "query_count": 0,
        "lane_count": 0,  # retired field
        "page_journal_count": 0,
        "notebook_set_hash": "",
        "lane_state_set_hash": "",  # retired field
        "page_journal_set_hash": "",
        "relevance_profile_hash": "",
        "store_schema_versions": {},
        "workspace_tree_sha256": "0" * 64,
        "migration_inventory_sha256": "",
    }
    raw = (json.dumps(old_manifest, indent=2, sort_keys=True) + "\n").encode()
    (gen_root / "workspace.json").write_bytes(raw)
    pointer = wsp.ActiveGenerationPointerV4(
        generation_id=gid,
        workspace_manifest_sha256=hashlib.sha256(raw).hexdigest(),
        activated_at="2026-07-23T08:08:58+00:00",
        migration_id="bootstrap-v4-init",
    )
    wsp._atomic_write_json(wsp.ACTIVE_GENERATION_PATH, pointer.to_dict())
    return gen_root


class TestResealActiveGenerationManifest:
    def test_lenient_generation_fails_strict_resolve(self, iso):
        _seed_lenient_generation(iso)
        with pytest.raises(wsp.WorkspaceManifestMismatchError):
            wsp.WorkspaceResolver().resolve_active()

    def test_dry_run_writes_nothing(self, iso):
        gen_root = _seed_lenient_generation(iso)
        before = (gen_root / "workspace.json").read_bytes()
        report = wsp.reseal_active_generation_manifest(apply=False)
        assert report["applied"] is False
        assert (gen_root / "workspace.json").read_bytes() == before
        new = report["new_manifest"]
        assert new["notebook_count"] == 1
        assert new["query_count"] == 2
        assert new["store_schema_versions"] == STORE_SCHEMA_VERSIONS_V4
        assert new["created_at"] == "2026-07-23T08:08:58+00:00"

    def test_apply_reseals_and_resolves_strict(self, iso):
        gen_root = _seed_lenient_generation(iso)
        report = wsp.reseal_active_generation_manifest(apply=True)
        assert report["applied"] is True
        assert report["verified"] is True
        # Strict resolve now passes, including tree verification.
        ws = wsp.WorkspaceResolver().resolve_active(verify_tree=True)
        assert ws.generation_id == "v4-lenient"
        # Pointer binds the NEW manifest bytes.
        pointer = json.loads(
            wsp.ACTIVE_GENERATION_PATH.read_text(encoding="utf-8")
        )
        assert pointer["workspace_manifest_sha256"] == hashlib.sha256(
            (gen_root / "workspace.json").read_bytes()
        ).hexdigest()
        assert pointer.get("previous_generation_id") is None
        # Retired fields are gone.
        new_manifest = json.loads(
            (gen_root / "workspace.json").read_text(encoding="utf-8")
        )
        assert "lane_count" not in new_manifest
        assert "lane_state_set_hash" not in new_manifest

    def test_unparseable_notebook_fails_closed(self, iso):
        gen_root = _seed_lenient_generation(iso)
        (gen_root / "keyword_notebooks" / "broken__xxxx.json").write_text(
            "{not json", encoding="utf-8"
        )
        before = (gen_root / "workspace.json").read_bytes()
        with pytest.raises(wsp.WorkspaceManifestMismatchError):
            wsp.reseal_active_generation_manifest(apply=True)
        assert (gen_root / "workspace.json").read_bytes() == before
