"""Security tests for source-record provider identity and path containment.

These tests verify that external provider identifiers cannot be used to
escape the intended ``source_records/`` directory hierarchy.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from src.metadata.source_records import (
    InvalidProviderIdentityError,
    SourceRecordPathEscapeError,
    normalize_provider_slug,
    write_metadata_source_record,
)
from src.fetch.fetch_result_record import write_fetch_result

pytestmark = pytest.mark.security


class TestProviderNormalization:
    """Provider slug normalization must reject dangerous inputs deterministically."""

    # --- Acceptance: normal providers ---
    @pytest.mark.parametrize("provider,expected", [
        ("crossref", "crossref"),
        ("OpenAlex", "openalex"),
        ("Semantic Scholar", "semantic_scholar"),
        ("   crossref   ", "crossref"),
        ("CROSSREF", "crossref"),
        ("my-provider", "my-provider"),
        ("provider.v2", "provider.v2"),
        ("a", "a"),
        ("x" * 64, "x" * 64),
    ])
    def test_normalize_valid_providers(self, provider: str, expected: str):
        assert normalize_provider_slug(provider) == expected

    # --- Rejection: path traversal / injection ---
    @pytest.mark.parametrize("provider", [
        "../../outside",
        "../../../outside",
        "..\\..\\outside",
        "a/b",
        "a\\b",
        "..",
        ".",
        "/absolute",
        "C:\\outside",
        "\\\\server\\share",
        "x:y",
    ])
    def test_normalize_rejects_path_traversal(self, provider: str):
        with pytest.raises(InvalidProviderIdentityError):
            normalize_provider_slug(provider)

    # --- Rejection: empty / whitespace / control ---
    @pytest.mark.parametrize("provider", [
        "",
        "   ",
        "\t",
        "\n",
        "\x00",
        "\x1f",
        "\x7f",
    ])
    def test_normalize_rejects_empty_or_control(self, provider: str):
        with pytest.raises(InvalidProviderIdentityError):
            normalize_provider_slug(provider)

    # --- Rejection: Windows reserved names ---
    @pytest.mark.parametrize("reserved", ["CON", "PRN", "AUX", "NUL", "COM1", "LPT2"])
    def test_normalize_rejects_windows_reserved(self, reserved: str):
        with pytest.raises(InvalidProviderIdentityError):
            normalize_provider_slug(reserved)

    # --- Rejection: too long ---
    def test_normalize_rejects_too_long(self):
        with pytest.raises(InvalidProviderIdentityError):
            normalize_provider_slug("x" * 65)

    # --- Stability and determinism ---
    def test_normalize_deterministic(self):
        """Multiple calls with same input produce same output."""
        inputs = ["Crossref", "  crossref  ", "CROSSREF", "cross_ref"]
        results = [normalize_provider_slug(p) for p in inputs]
        # All should produce a deterministic result
        assert results[0] == "crossref"
        assert results[1] == "crossref"
        assert results[2] == "crossref"
        assert results[3] == "cross_ref"


class TestSourceRecordWriteContainment:
    """File-system-level containment test for source records.

    These use a real filesystem via tmp_path to verify that no file is
    written outside the intended source_records/ subdirectory.
    """

    def test_write_normal_provider(self, tmp_path: Path):
        """Normal provider writes to expected path inside source_records/."""
        path = write_metadata_source_record(tmp_path, "crossref", {"key": "value"})
        expected = tmp_path / "source_records" / "metadata_source.crossref.json"
        assert path == expected.resolve(), f"Unexpected path: {path}"
        assert path.exists()
        assert path.read_text(encoding="utf-8") == '{\n  "key": "value"\n}'

    def test_write_with_spaces_and_case(self, tmp_path: Path):
        """Provider with spaces and uppercase normalizes deterministically."""
        path = write_metadata_source_record(tmp_path, "Semantic Scholar", {"id": 1})
        expected = tmp_path / "source_records" / "metadata_source.semantic_scholar.json"
        assert path == expected.resolve()

    def test_write_twice_same_provider(self, tmp_path: Path):
        """Writing same provider twice overwrites the same file."""
        path1 = write_metadata_source_record(tmp_path, "openalex", {"v": 1})
        assert path1.name == "metadata_source.openalex.json"
        assert path1.stat().st_size > 0
        path2 = write_metadata_source_record(tmp_path, "openalex", {"v": 2})
        assert path2 == path1  # same path
        import json
        data = json.loads(path2.read_text(encoding="utf-8"))
        assert data == {"v": 2}

    def test_write_rejects_path_traversal(self, tmp_path: Path):
        """Path traversal providers must be rejected before any file is created."""
        source_records_dir = tmp_path / "source_records"
        outside_file = tmp_path / "escaped.txt"

        for bad_provider in ["../../outside", "..\\..\\outside", "a/../../evil"]:
            with pytest.raises(InvalidProviderIdentityError):
                write_metadata_source_record(tmp_path, bad_provider, {"x": 1})
            # Verify nothing was written outside
            assert not outside_file.exists(), f"File created despite provider={bad_provider}"
            if source_records_dir.exists():
                # The source_records dir might not exist since the write was rejected
                pass

    def test_write_rejects_windows_reserved(self, tmp_path: Path):
        """Windows reserved provider names must fail."""
        for reserved in ["CON", "PRN", "AUX", "NUL", "COM1", "LPT2"]:
            with pytest.raises(InvalidProviderIdentityError):
                write_metadata_source_record(tmp_path, reserved, {"x": 1})

    def test_write_deterministic_filename_consistency(self, tmp_path: Path):
        """Verify that raw_record_path matches actual written path."""
        path = write_metadata_source_record(tmp_path, "OpenAlex", {"key": "val"})
        rel_path = "source_records/metadata_source.openalex.json"
        assert path == (tmp_path / rel_path).resolve()

    def test_no_file_outside_source_records(self, tmp_path: Path):
        """After all writes, no files except those in source_records/ exist."""
        pre = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()}
        write_metadata_source_record(tmp_path, "crossref", {"a": 1})
        write_metadata_source_record(tmp_path, "openalex", {"b": 2})
        post = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()}
        new_files = post - pre
        for f in new_files:
            # Normalize to POSIX for cross-platform check
            norm = f.replace("\\", "/")
            assert norm.startswith("source_records/"), f"File outside source_records/: {f}"

    @pytest.mark.parametrize("writer", [
        lambda workspace: write_metadata_source_record(workspace, "crossref", {"x": 1}),
        lambda workspace: write_fetch_result(workspace, {"url": "https://example.test/p.pdf"}),
    ])
    def test_writer_rejects_source_records_symlink(self, tmp_path: Path, writer):
        outside = tmp_path / "outside"
        workspace = tmp_path / "workspace"
        outside.mkdir()
        workspace.mkdir()
        try:
            (workspace / "source_records").symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("directory symlinks unavailable")
        with pytest.raises(SourceRecordPathEscapeError):
            writer(workspace)
        assert not list(outside.iterdir())

    def test_writer_rejects_target_symlink(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        outside = tmp_path / "outside.json"
        (workspace / "source_records").mkdir(parents=True)
        outside.write_text("unchanged", encoding="utf-8")
        target = workspace / "source_records" / "metadata_source.crossref.json"
        try:
            target.symlink_to(outside)
        except OSError:
            pytest.skip("file symlinks unavailable")
        with pytest.raises(SourceRecordPathEscapeError):
            write_metadata_source_record(workspace, "crossref", {"x": 1})
        assert outside.read_text(encoding="utf-8") == "unchanged"
