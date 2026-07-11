"""Test that the packer produces a valid, self-verifying snapshot ZIP.

These tests import the packer's public components directly (not via
subprocess) to verify the runtime-zero contract. The full end-to-end
pack → self-check → verify cycle is covered by agent_acceptance.py.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from scripts.pack_repo import (
    REQUIRED_ROOT_FILES,
    SnapshotMember,
    _member_digest,
    _verify_snapshot_self_check,
    _should_pack,
    _safe_for_zip,
)

pytestmark = pytest.mark.e2e


def _make_minimal_valid_manifest(members: list[str]) -> dict:
    """Build a manifest dict that passes ``_verify_snapshot_self_check``."""
    planned = sorted(
        [SnapshotMember(name, 4, "3a6eb0790f39ac87c94f3856b2dd2c5d110e6811602261a9a923d3bb23adc8b7") for name in members],
        key=lambda member: member.path,
    )
    return {
        "schema_version": "2.0",
        "snapshot_type": "runtime_zero_source_audit",
        "payload_file_count": len(members),
        "runtime_files_included": 0,
        "runtime_workspaces_included": {"paper_raw": 0, "papers": 0},
        "selection": {"count": len(planned), "sha256": _member_digest(planned)},
        "archive": {"count": len(planned), "sha256": _member_digest(planned)},
        "omissions": [],
        "members": [member.__dict__ for member in planned],
        "verification": {"runtime_zero": True, "secret_scan": "passed", "archive_member_check": "passed"},
    }


# ── Snapshot self-check ────────────────────────────────────────────────


class TestSnapshotSelfCheck:
    """``_verify_snapshot_self_check`` owns the runtime-zero contract."""

    def _make_valid_zip(self, path: Path, members: list[str]) -> Path:
        zf_path = path / "test_snapshot.zip"
        with zipfile.ZipFile(zf_path, "w") as zf:
            for member in members:
                zf.writestr(member, "data")
            zf.writestr(
                "snapshot_manifest.json",
                json.dumps(_make_minimal_valid_manifest(members)),
            )
        return zf_path

    def test_valid_snapshot_passes(self, tmp_path: Path) -> None:
        members = list(REQUIRED_ROOT_FILES)
        zf_path = self._make_valid_zip(tmp_path, members)
        errors = _verify_snapshot_self_check(zf_path)
        assert errors == [], f"unexpected errors: {errors}"

    def test_missing_required_root_file_fails(self, tmp_path: Path) -> None:
        members = [f for f in REQUIRED_ROOT_FILES if f != "LICENSE"]
        zf_path = self._make_valid_zip(tmp_path, members)
        errors = _verify_snapshot_self_check(zf_path)
        assert any("LICENSE" in e for e in errors), f"expected LICENSE error: {errors}"

    def test_absolute_path_rejected(self, tmp_path: Path) -> None:
        zf_path = tmp_path / "bad.zip"
        members = list(REQUIRED_ROOT_FILES) + ["/etc/passwd"]
        with zipfile.ZipFile(zf_path, "w") as zf:
            for member in members:
                zf.writestr(member, "data")
            zf.writestr(
                "snapshot_manifest.json",
                json.dumps(_make_minimal_valid_manifest(members)),
            )
        errors = _verify_snapshot_self_check(zf_path)
        assert any("absolute" in e for e in errors)

    def test_path_escape_rejected(self, tmp_path: Path) -> None:
        zf_path = tmp_path / "escape.zip"
        members = list(REQUIRED_ROOT_FILES) + ["../../etc/passwd"]
        with zipfile.ZipFile(zf_path, "w") as zf:
            for member in members:
                zf.writestr(member, "data")
            zf.writestr(
                "snapshot_manifest.json",
                json.dumps(_make_minimal_valid_manifest(members)),
            )
        errors = _verify_snapshot_self_check(zf_path)
        assert any("escape" in e for e in errors)

    def test_duplicate_member_rejected(self, tmp_path: Path) -> None:
        zf_path = tmp_path / "dup.zip"
        members = list(REQUIRED_ROOT_FILES)
        with zipfile.ZipFile(zf_path, "w") as zf:
            for member in members:
                zf.writestr(member, "data")
            # Write LICENSE twice
            zf.writestr("LICENSE", "second copy")
            zf.writestr(
                "snapshot_manifest.json",
                json.dumps(_make_minimal_valid_manifest(members)),
            )
        errors = _verify_snapshot_self_check(zf_path)
        assert any("duplicate" in e for e in errors)

    def test_missing_manifest_fails(self, tmp_path: Path) -> None:
        zf_path = tmp_path / "no_manifest.zip"
        with zipfile.ZipFile(zf_path, "w") as zf:
            zf.writestr("LICENSE", "data")
        errors = _verify_snapshot_self_check(zf_path)
        assert any("missing" in e for e in errors), f"unexpected: {errors}"


# ── Packer path utilities ──────────────────────────────────────────────


class TestPackerPathUtilities:
    """``_should_pack`` and ``_safe_for_zip`` filter correctly."""

    def test_safe_for_zip_utf8(self) -> None:
        assert _safe_for_zip("src/main.py") is True

    def test_safe_for_zip_surrogate(self) -> None:
        assert _safe_for_zip("src/bad\ud800file.py") is False

    def test_should_pack_source_py(self) -> None:
        assert _should_pack("src/ingest/commit.py") is True

    def test_should_pack_runtime_dir(self) -> None:
        assert _should_pack("data/raw/some_paper.pdf") is False

    def test_should_pack_excluded_path(self) -> None:
        assert _should_pack("output/report.pdf") is False

    def test_should_pack_pdf_in_src(self) -> None:
        assert _should_pack("src/data.pdf") is False


# ── Real snapshot verification ─────────────────────────────────────────


class TestRealSnapshot:
    """Verify the snapshot produced by the agent_acceptance run."""

    SNAPSHOT_PATH = Path("mineru_snapshot.zip")

    def test_snapshot_exists_and_readable(self) -> None:
        if not self.SNAPSHOT_PATH.exists():
            pytest.skip("snapshot not present (run agent_acceptance first)")
        with zipfile.ZipFile(self.SNAPSHOT_PATH, "r") as zf:
            assert zf.namelist()

    def test_snapshot_passes_self_check(self) -> None:
        if not self.SNAPSHOT_PATH.exists():
            pytest.skip("snapshot not present")
        errors = _verify_snapshot_self_check(self.SNAPSHOT_PATH)
        assert errors == [], f"snapshot self-check failed: {errors}"

    def test_snapshot_has_required_root_files(self) -> None:
        if not self.SNAPSHOT_PATH.exists():
            pytest.skip("snapshot not present")
        with zipfile.ZipFile(self.SNAPSHOT_PATH, "r") as zf:
            names = set(zf.namelist())
        missing = REQUIRED_ROOT_FILES - names
        assert not missing, f"missing root files: {missing}"

    def test_snapshot_manifest_is_json(self) -> None:
        if not self.SNAPSHOT_PATH.exists():
            pytest.skip("snapshot not present")
        with zipfile.ZipFile(self.SNAPSHOT_PATH, "r") as zf:
            manifest = json.loads(zf.read("snapshot_manifest.json"))
        assert manifest["runtime_files_included"] == 0
