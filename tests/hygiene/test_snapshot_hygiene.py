from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.agent_acceptance import verify_git_hygiene, verify_root_hygiene, verify_snapshot


pytestmark = [pytest.mark.hygiene, pytest.mark.security]


def _make_zip(names: list[str], tmp_path: Path) -> Path:
    zip_path = tmp_path / "mineru_snapshot.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name in names:
            zf.writestr(name, b"")
    return zip_path


# ── verify_snapshot: forbidden paths ──────────────────────────────────


def test_snapshot_verifier_rejects_output_and_reports(tmp_path):
    _make_zip([
        "output/mineru_cache/x/a.md",
        "reports/run.json",
    ], tmp_path)
    errors = verify_snapshot(root_path=tmp_path)
    assert any("output/mineru_cache/x/a.md" in error for error in errors)
    assert any("reports/run.json" in error for error in errors)


def test_snapshot_verifier_allows_runtime_gitkeeps(tmp_path):
    _make_zip([
        "data/papers/.gitkeep",
        "data/import_work/.gitkeep",
        "write/jobs/.gitkeep",
    ], tmp_path)
    assert verify_snapshot(root_path=tmp_path) == []


def test_snapshot_verifier_rejects_import_work_runtime_file(tmp_path):
    _make_zip([
        "data/import_work/foo.pdf",
        "data/import_work/.gitkeep",
    ], tmp_path)
    errors = verify_snapshot(root_path=tmp_path)
    assert any("data/import_work/foo.pdf" in error for error in errors)


def test_snapshot_verifier_rejects_non_pdf_runtime_file(tmp_path):
    _make_zip([
        "data/import_work/bar.txt",
        "data/import_work/.gitkeep",
    ], tmp_path)
    errors = verify_snapshot(root_path=tmp_path)
    assert any("data/import_work/bar.txt" in error for error in errors)


def test_snapshot_verifier_allows_non_runtime_source_paths(tmp_path):
    _make_zip([
        "src/main.py",
        "scripts/foo.py",
        "tests/test_x.py",
        "docs/readme.md",
    ], tmp_path)
    assert verify_snapshot(root_path=tmp_path) == []


def test_snapshot_verifier_rejects_reasonix(tmp_path):
    _make_zip([
        ".reasonix/autoresearch/job/state/progress.json",
        ".reasonix/autoresearch/job/logs/heartbeat.jsonl",
        ".reasonix/desktop-topic-titles.json",
    ], tmp_path)
    errors = verify_snapshot(root_path=tmp_path)
    assert any(".reasonix/" in error for error in errors), errors


def test_snapshot_verifier_rejects_tombstone_files(tmp_path):
    _make_zip([
        "tests/test_old_thing.py._deleted",
        "tests/test_another.py._deleted",
        "src/module.py._deleted",
    ], tmp_path)
    errors = verify_snapshot(root_path=tmp_path)
    assert any("tombstone" in error for error in errors), errors
    assert len(errors) == 3, errors


def test_root_hygiene_rejects_debug_leftovers(tmp_path):
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "loopback_check3.txt").write_text("debug\n", encoding="utf-8")
    (tmp_path / "_check_syntax.py").write_text("print('debug')\n", encoding="utf-8")
    (tmp_path / "trace_run.log").write_text("trace\n", encoding="utf-8")

    errors = verify_root_hygiene(root_path=tmp_path)

    assert any("loopback_check3.txt" in err for err in errors)
    assert any("_check_syntax.py" in err for err in errors)
    assert any("trace_run.log" in err for err in errors)


# ── verify_snapshot: audit-allowlisted runtime samples ────────────────


def test_snapshot_verifier_allows_paper_data_json_and_md(tmp_path):
    """Audit snapshot allows .json / .md / .paper.number from paper data dirs."""
    _make_zip([
        "data/papers/2024_x/paper.metadata.json",
        "data/papers/2024_x/paper.catalog.json",
        "data/papers/2024_x/paper.md",
        "data/papers/2024_x/source_records/metadata_source.crossref.json",
        "data/paper_raw/0000000000000001/0000000000000001.md",
        "data/paper_raw/0000000000000001/.import_status.json",
        "data/paper_raw/0000000000000001/stage_manifest.json",
    ], tmp_path)
    errors = verify_snapshot(root_path=tmp_path)
    assert errors == [], f"unexpected errors: {errors}"


def test_snapshot_verifier_rejects_paper_data_non_allowlisted(tmp_path):
    """Audit snapshot rejects non-allowlisted suffixes from paper data dirs."""
    _make_zip([
        "data/papers/2024_x/model.pt",
        "data/papers/2024_x/data.pkl",
        "data/paper_raw/00000001/data.bin",
    ], tmp_path)
    errors = verify_snapshot(root_path=tmp_path)
    assert any(".pt" in error for error in errors)
    assert any(".pkl" in error for error in errors)


def test_snapshot_verifier_allows_paper_number_marker(tmp_path):
    """Audit snapshot allows .paper.number markers under runtime sample dirs."""
    _make_zip([
        "data/papers/2024_x/0000000000000001.paper.number",
    ], tmp_path)
    assert verify_snapshot(root_path=tmp_path) == []


def test_snapshot_verifier_rejects_non_lightweight_runtime_sample(tmp_path):
    """Audit snapshot rejects non-lightweight files under runtime sample dirs
    even when the suffix is not in the heavy/binary deny list (e.g. .exe)."""
    _make_zip([
        "data/papers/2024_x/file.exe",
    ], tmp_path)
    errors = verify_snapshot(root_path=tmp_path)
    assert any("non-lightweight runtime sample" in e for e in errors), errors


# ── verify_snapshot: denylist (secrets, cache, venv) ──────────────────


def test_snapshot_verifier_rejects_env_file(tmp_path):
    _make_zip(["data/papers/x/.env"], tmp_path)
    errors = verify_snapshot(root_path=tmp_path)
    assert any(".env" in error for error in errors)


def test_snapshot_verifier_rejects_credentials_json(tmp_path):
    _make_zip(["data/papers/x/credentials.json"], tmp_path)
    errors = verify_snapshot(root_path=tmp_path)
    assert any("credentials.json" in error for error in errors)


def test_snapshot_verifier_rejects_git_dir(tmp_path):
    _make_zip(["data/papers/x/.git/config"], tmp_path)
    errors = verify_snapshot(root_path=tmp_path)
    assert any(".git" in error for error in errors)


def test_snapshot_verifier_rejects_cache_dir(tmp_path):
    _make_zip(["data/papers/x/__pycache__/cache.pyc"], tmp_path)
    errors = verify_snapshot(root_path=tmp_path)
    assert any("__pycache__" in error for error in errors)


# ── verify_git_hygiene ────────────────────────────────────────────────


def test_git_hygiene_accepts_clean_git_index(tmp_path):
    """verify_git_hygiene() must accept a clean index (no runtime assets).

    Mocked so the test does not depend on the real repo's ``.git`` — the
    audit-zip extraction environment has no ``.git`` directory, and a real
    ``git ls-files`` there fails with "not a git repository".
    """
    with patch("scripts.agent_acceptance.subprocess.run") as mock_run:
        class FakeResult:
            returncode = 0
            stdout = "README.md\nsrc/main.py\nscripts/pack_repo.py\n"
            stderr = ""
        mock_run.return_value = FakeResult()

        errors = verify_git_hygiene(root_path=tmp_path)
        assert errors == [], f"git hygiene errors: {errors}"


@pytest.mark.parametrize("simulated_file", [
    "data/papers/2024_x/paper.md",
    "data/paper_raw/0000000000000001/paper.json",
    "data/raw/sample.pdf",
    "data/logs/run.log",
    "data/jobs/debug.txt",
    "output/mineru_cache/x/a.md",
    "reports/run.json",
])
def test_git_hygiene_rejects_runtime_assets(simulated_file, tmp_path):
    """verify_git_hygiene() must reject runtime assets in git index."""
    with patch("scripts.agent_acceptance.subprocess.run") as mock_run:
        class FakeResult:
            returncode = 0
            stdout = simulated_file
            stderr = ""
        mock_run.return_value = FakeResult()

        errors = verify_git_hygiene(root_path=tmp_path)
        assert any(simulated_file in err for err in errors), f"expected {simulated_file} in errors: {errors}"


# ── Integration / acceptance flow ──────────────────────────────────────


def test_agent_acceptance_runs_pack_after_pytest():
    root = Path(__file__).resolve().parents[2]
    text = (root / "scripts" / "agent_acceptance.py").read_text(encoding="utf-8")
    assert "pytest" in text
    assert "pack_repo.py" in text
    assert text.index("pytest") < text.index("pack_repo.py"), (
        "pack_repo.py must run after pytest in agent_acceptance.py"
    )


def test_agent_acceptance_exists():
    root = Path(__file__).resolve().parents[2]
    assert (root / "scripts" / "agent_acceptance.py").exists()


def test_gitignore_matches_pack_repo_runtime_dirs():
    root = Path(__file__).resolve().parents[2]
    text = (root / ".gitignore").read_text(encoding="utf-8")
    for pattern in [
        "data/raw_all/**",
        "data/discovery/queries/**",
        "!data/raw_all/.gitkeep",
        "!data/discovery/queries/.gitkeep",
        # Local tooling state and tombstones must be gitignored
        ".reasonix/",
        "*._deleted",
    ]:
        assert pattern in text, f".gitignore missing: {pattern}"
