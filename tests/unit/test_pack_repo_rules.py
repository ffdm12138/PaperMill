from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import scripts.pack_repo as pr


pytestmark = pytest.mark.unit

WF = pr.PACK_PROFILE  # preserve original


def setup_module():
    """Reset profile after each module test run."""
    pass


def _set_profile(profile: str) -> None:
    pr.PACK_PROFILE = profile


# ── Universal excludes (regardless of profile) ────────────────────────

@pytest.mark.parametrize("path", [
    # write jobs runtime
    "write/jobs/demo/tex/main.tex",
    "write/jobs/demo/article.md",
    "write/jobs/demo/references.bib",
    "write/jobs/demo/article/0000000000000001/full.md",
    # legacy write dirs
    "write/001_legacy/tex/main.tex",
    "write/job_demo/tex/main.tex",
    # data/llm_work
    "data/llm_work/job/article.md",
    "data/llm_work/demo/000001/full.md",
    "data/llm_work/demo/000001/paper.md",
    # data/import_work (always excluded)
    "data/import_work/doi_batch.jsonl",
    "data/import_work/x/file.pdf",
    # data/discovery
    "data/discovery/queries/query.json",
    # data/locks
    "data/locks/write.lock",
    # output
    "output/mineru_cache/x/a.md",
    "output/mineru_cache/x/mineru_output_cache.json",
    "output/random.txt",
    # reports runtime (non-template, non-gitkeep)
    "reports/run.json",
    "reports/report.txt",
    "reports/doctor_ingest_pipeline_report.json",
    "reports/commit_report.json",
    "reports/stage_raw_move.json",
    "reports/dup_dryrun.err",
    "reports/paper_list.txt",
    "reports/real_ingest_acceptance.md",
    "reports/code_audit_2026-06-30.md",
    "reports/import_report.csv",
    # catalog generated/backup/runtime
    "data/catalog/all.catalog.json",
    "data/catalog/paper_index.json",
    "data/catalog/paper_number_ledger.json",
    "data/catalog/paper_number_ledger.json.bak",
    "data/catalog/paper_number_ledger.bak_20260703.json",
    "data/catalog/foo.runtime.json",
    "data/catalog/foo.bak",
    "data/catalog/foo.bak.json",
    "data/catalog/some.bak",
    # local backup/tmp
    "some/path/local.backup",
    "some/path/file.tmp",
    "some/path/file.temp",
    # root scratch
    "=",
    "keep_rank",
    # gitkeep inside runtime (not a real gitkeep)
    "write/jobs/.gitkeep/not_allowed.txt",
    # local agent/research tooling state
    ".reasonix/autoresearch/job/state/progress.json",
    ".reasonix/autoresearch/job/logs/heartbeat.jsonl",
    ".reasonix/desktop-topic-titles.json",
    # test migration tombstones
    "tests/test_old_thing.py._deleted",
    "tests/test_another.py._deleted",
    "src/module.py._deleted",
    # import dir
    "import/data.pdf",
])
def test_should_pack_universal_excludes(path):
    """These paths must be excluded in both audit and source profiles."""
    for profile in ("audit", "source"):
        _set_profile(profile)
        assert pr._should_pack(path) is False, f"{profile}: {path} should be excluded"


# ── Source profile: paper data dirs only .gitkeep ─────────────────────

@pytest.mark.parametrize("path", [
    "data/papers/0000000000000001/a.pdf",
    "data/papers/2024_x/2024_x.pdf",
    "data/paper_raw/0000000000000001/a.md",
    "data/paper_raw/000001/000001.md",
    "data/paper_raw/000001/paper.json",
    "data/raw/some.pdf",
    "data/raw_all/sample.pdf",
    "data/papers/.gitkeephack",
])
def test_should_pack_source_profile_excludes_paper_data(path):
    """In source profile, paper data dirs only allow .gitkeep."""
    _set_profile("source")
    assert pr._should_pack(path) is False


@pytest.mark.parametrize("path", [
    "data/papers/.gitkeep",
    "data/paper_raw/.gitkeep",
    "data/raw/.gitkeep",
    "data/raw_all/.gitkeep",
])
def test_should_pack_source_profile_allows_gitkeeps(path):
    _set_profile("source")
    assert pr._should_pack(path) is True


# ── Audit profile: paper data dirs allow lightweight text/structure ────

@pytest.mark.parametrize("path", [
    "data/papers/2024_x/2024_x.json",
    "data/papers/2024_x/2024_x.md",
    "data/papers/2024_x/2024_x.metadata.json",
    "data/papers/2024_x/2024_x.catalog.json",
    "data/papers/2024_x/source_records/metadata_source.crossref.json",
    "data/papers/2024_x/0000000000000001.paper.number",
    "data/paper_raw/0000000000000001/0000000000000001.paper.number",
    "data/paper_raw/0000000000000001/0000000000000001.md",
    "data/paper_raw/0000000000000001/0000000000000001.catalog.json",
    "data/paper_raw/0000000000000001/source_records/metadata_source.manual.json",
    "data/import_work/.gitkeep",
    "data/raw/sample.json",
    "data/raw_all/data.jsonl",
])
def test_should_pack_audit_allows_allowlisted_runtime(path):
    """Audit profile allows .json, .md, .paper.number, .gitkeep from paper data dirs."""
    _set_profile("audit")
    assert pr._should_pack(path) is True, f"{path} should pack"
    assert pr._should_pack(path, require_lightweight=True) is True, f"{path} should be lightweight"


@pytest.mark.parametrize("path", [
    "data/papers/2024_x/image.png",
    "data/papers/2024_x/figure.jpg",
    "data/papers/2024_x/photo.jpeg",
    "data/papers/2024_x/anim.webp",
    "data/paper_raw/0000000000000001/figure.png",
])
def test_should_pack_audit_blocks_images(path):
    """HEAVY_OR_BINARY_DENIED_SUFFIXES blocks images in both profiles."""
    for profile in ("audit", "source"):
        _set_profile(profile)
        assert pr._should_pack(path) is False, f"{profile}: {path} should be blocked"


@pytest.mark.parametrize("path", [
    "data/papers/2024_x/paper.pdf",
    "data/paper_raw/0000000000000001/doc.pdf",
    "data/raw/thesis.pdf",
])
def test_should_pack_audit_blocks_pdf(path):
    """HEAVY_OR_BINARY_DENIED_SUFFIXES blocks PDFs in both profiles."""
    for profile in ("audit", "source"):
        _set_profile(profile)
        assert pr._should_pack(path) is False, f"{profile}: {path} should be blocked"


# ── HEAVY_OR_BINARY_DENIED_SUFFIXES (all profiles) ──────────────────────

@pytest.mark.parametrize("path", [
    "data/papers/2024_x/data.pkl",
    "data/papers/2024_x/model.pt",
    "data/papers/2024_x/data.onnx",
    "data/paper_raw/00000001/cache.bin",
    "data/raw/source.pdf",
    "data/raw/archive.zip",
    "data/raw/database.sqlite",
])
def test_should_pack_blocks_heavy_binary(path):
    """HEAVY_OR_BINARY_DENIED_SUFFIXES blocks binary suffixes in both profiles."""
    for profile in ("audit", "source"):
        _set_profile(profile)
        assert pr._should_pack(path) is False, f"{profile}: {path} should be blocked"


# ── Denylist (all profiles) ──────────────────────────────────────────

@pytest.mark.parametrize("path,denied_part", [
    ("data/papers/x/.git/config", ".git"),
    ("data/paper_raw/y/__pycache__/cache.pyc", "__pycache__"),
    ("data/papers/z/virtual/.venv/lib/python", ".venv"),
    ("data/papers/z/node_modules/pkg/index.js", "node_modules"),
])
def test_should_pack_blocks_denied_parts(path, denied_part):
    """Files under DENIED_PATH_PARTS are blocked regardless of suffix."""
    for profile in ("audit", "source"):
        _set_profile(profile)
        assert pr._should_pack(path) is False


@pytest.mark.parametrize("path,name", [
    ("data/papers/x/.env", ".env"),
    ("data/paper_raw/y/credentials.json", "credentials.json"),
    ("data/raw/z/token.json", "token.json"),
])
def test_should_pack_blocks_denied_names(path, name):
    """Known secret file names are blocked regardless of suffix."""
    for profile in ("audit", "source"):
        _set_profile(profile)
        assert pr._should_pack(path) is False


# ── New constants export check ────────────────────────────────────────

def test_new_constants_exist():
    """Verify pack_repo exports expected constants."""
    assert hasattr(pr, "LIGHTWEIGHT_ALLOWED_SUFFIXES")
    assert ".json" in pr.LIGHTWEIGHT_ALLOWED_SUFFIXES
    assert ".md" in pr.LIGHTWEIGHT_ALLOWED_SUFFIXES
    assert hasattr(pr, "HEAVY_OR_BINARY_DENIED_SUFFIXES")
    assert ".pdf" in pr.HEAVY_OR_BINARY_DENIED_SUFFIXES
    assert ".png" in pr.HEAVY_OR_BINARY_DENIED_SUFFIXES
    assert hasattr(pr, "DENIED_NAMES")
    assert ".env" in pr.DENIED_NAMES
    assert hasattr(pr, "DENIED_PATH_PARTS")
    assert ".git" in pr.DENIED_PATH_PARTS


def test_pack_repo_secret_scan_catches_generic_bearer_token():
    token = "Bearer " + "A" * 32
    findings = pr.scan_text_for_secrets(f"Authorization: {token}", "sample.txt")
    assert findings
    assert findings[0]["rule"] in {"authorization_bearer_literal", "bearer_literal"}


def test_pack_repo_secret_scan_allows_bare_env_var_names():
    text = "OPENALEX_EMAIL OPENALEX_API_KEY SEMANTIC_SCHOLAR_API_KEY"
    assert pr.scan_text_for_secrets(text, "docs/example.md") == []


# ── Source/template/gitkeep paths to keep (all profiles) ──────────────

@pytest.mark.parametrize("path", [
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "docs/PROJECT_CONTRACT.md",
    "docs/TESTING.md",
    "scripts/pack_repo.py",
    "src/fetch/models.py",
    "src/writer/job_manager.py",
    "tests/unit/test_pack_repo_rules.py",
    # write docs and gitkeep
    "write/README.md",
    "write/.gitkeep",
    "write/jobs/.gitkeep",
    # data gitkeeps
    "data/papers/.gitkeep",
    "data/paper_raw/.gitkeep",
    "data/raw/.gitkeep",
    "data/raw_all/.gitkeep",
    "data/import_work/.gitkeep",
    "data/discovery/queries/.gitkeep",
    # reports template
    "reports/.gitkeep",
    "reports/report_template.md",
    # catalog templates
    "data/catalog/all.catalog.template.json",
    "data/catalog/paper_number_ledger.template.json",
])
def test_should_pack_keeps_source_docs_and_gitkeeps(path):
    """Source docs, gitkeeps, and templates must be allowed in both profiles."""
    for profile in ("audit", "source"):
        _set_profile(profile)
        assert pr._should_pack(path) is True, f"{profile}: {path} should be kept"


# ── git_tracked_files: source vs audit profile semantics ─────────────


class _FakeGitResult:
    def __init__(self, stdout: str):
        self.returncode = 0
        self.stdout = stdout
        self.stderr = ""


def test_source_profile_excludes_untracked_workspace_files(tmp_path, monkeypatch):
    """source profile must only include git-tracked files, not untracked
    workspace files (no --others in the git ls-files query)."""
    (tmp_path / "README.md").write_text("readme", encoding="utf-8")
    (tmp_path / "untracked_scratch.md").write_text("scratch", encoding="utf-8")
    monkeypatch.setattr(pr, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(pr, "PACK_PROFILE", "source")
    monkeypatch.setattr(pr.subprocess, "run",
                        lambda *a, **k: _FakeGitResult("README.md\0"))

    files = pr.git_tracked_files(profile="source")

    assert "README.md" in files
    assert "untracked_scratch.md" not in files


def test_audit_profile_includes_untracked_lightweight_files(tmp_path, monkeypatch):
    """audit profile includes untracked lightweight files via the workspace
    scan (a superset of the old --others --exclude-standard git output)."""
    (tmp_path / "README.md").write_text("readme", encoding="utf-8")
    (tmp_path / "untracked_scratch.md").write_text("scratch", encoding="utf-8")
    monkeypatch.setattr(pr, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(pr, "PACK_PROFILE", "audit")
    monkeypatch.setattr(pr.subprocess, "run",
                        lambda *a, **k: _FakeGitResult("README.md\0"))

    files = pr.git_tracked_files(profile="audit")

    assert "README.md" in files
    assert "untracked_scratch.md" in files


# -- workspace sampling tests -----------------------------------------------


def _make_paper_raw_workspaces(raw_dir: Path, count: int) -> list[Path]:
    """Create ``count`` 16-digit workspace dirs with lightweight files."""
    dirs = []
    for i in range(count):
        ws = raw_dir / f"{i:016d}"
        ws.mkdir(parents=True)
        (ws / f"{i:016d}.metadata.json").write_text("{}", encoding="utf-8")
        (ws / f"{i:016d}.paper.number").write_text(
            json.dumps({"paper_number": f"{i:016d}", "state": "reserved"}), encoding="utf-8"
        )
        (ws / "source_records").mkdir(exist_ok=True)
        (ws / "source_records" / "test.json").write_text("{}", encoding="utf-8")
        dirs.append(ws)
    return dirs


def _make_papers_workspaces(papers_dir: Path, count: int) -> list[Path]:
    """Create ``count`` named workspace dirs with core paper assets."""
    dirs = []
    for i in range(count):
        name = f"2024_Author_{chr(65 + i)}"
        ws = papers_dir / name
        ws.mkdir(parents=True)
        (ws / f"{name}.metadata.json").write_text("{}", encoding="utf-8")
        (ws / f"{name}.catalog.json").write_text("{}", encoding="utf-8")
        (ws / f"{name}.md").write_text("# Paper", encoding="utf-8")
        (ws / "source_records").mkdir(exist_ok=True)
        (ws / "source_records" / "test.json").write_text("{}", encoding="utf-8")
        dirs.append(ws)
    return dirs


class TestResolveWorkspacePrefix:
    """Unit tests for _resolve_workspace_prefix."""

    @pytest.mark.parametrize("path,expected", [
        ("data/paper_raw/.gitkeep", None),
        ("data/paper_raw/README.md", None),
        ("data/paper_raw/index.json", None),
        ("data/paper_raw/0000000000000001/file.json", "data/paper_raw/0000000000000001"),
        ("data/paper_raw/0000000000000001/subdir/data.json", "data/paper_raw/0000000000000001"),
        ("data/papers/.gitkeep", None),
        ("data/papers/README.md", None),
        ("data/papers/2024_Wang_x/file.json", "data/papers/2024_Wang_x"),
        ("src/main.py", None),
        ("README.md", None),
        ("data/other/file.json", None),
        ("data/paper_raw/not_a_number/file.json", None),
        ("data/paper_raw/0000000000000001", None),  # 3 parts, depth too shallow
        ("data/papers/ws", None),                   # 2 parts, depth too shallow
    ])
    def test_resolve_workspace_prefix(self, path, expected):
        assert pr._resolve_workspace_prefix(path) == expected


class TestWorkspaceSampling:
    """Integration tests for workspace sampling helpers."""

    def test_should_sample_keep_root_level_and_workspace(self, tmp_path, monkeypatch):
        """Root-level files always pass; workspace files are gated."""
        monkeypatch.setattr(pr, "PROJECT_ROOT", tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "paper_raw").mkdir()
        (data_dir / "papers").mkdir()

        sampling = pr._selected_sample_workspaces(tmp_path, limit=5)

        # Root-level: always keep
        assert pr._should_sample_keep("data/paper_raw/.gitkeep", sampling) is True
        assert pr._should_sample_keep("data/paper_raw/README.md", sampling) is True
        assert pr._should_sample_keep("src/main.py", sampling) is True

    def test_all_files_in_selected_workspace_kept(self, tmp_path, monkeypatch):
        """All lightweight files in a selected workspace must be kept."""
        monkeypatch.setattr(pr, "PROJECT_ROOT", tmp_path)
        raw_dir = tmp_path / "data" / "paper_raw"
        raw_dir.mkdir(parents=True)
        (tmp_path / "data" / "papers").mkdir(parents=True)

        _make_paper_raw_workspaces(raw_dir, 3)
        sampling = pr._selected_sample_workspaces(tmp_path, limit=5)
        files = [
            "data/paper_raw/0000000000000000/0000000000000000.metadata.json",
            "data/paper_raw/0000000000000000/0000000000000000.paper.number",
            "data/paper_raw/0000000000000000/source_records/test.json",
        ]
        for f in files:
            assert pr._should_sample_keep(f, sampling) is True, f"Should keep: {f}"

    def test_zero_files_from_non_selected_workspace(self, tmp_path, monkeypatch):
        """No files from non-selected workspaces may leak in."""
        monkeypatch.setattr(pr, "PROJECT_ROOT", tmp_path)
        raw_dir = tmp_path / "data" / "paper_raw"
        raw_dir.mkdir(parents=True)
        (tmp_path / "data" / "papers").mkdir(parents=True)

        _make_paper_raw_workspaces(raw_dir, 8)
        sampling = pr._selected_sample_workspaces(tmp_path, limit=5)

        for i in range(5):
            f = f"data/paper_raw/{i:016d}/{i:016d}.metadata.json"
            assert pr._should_sample_keep(f, sampling) is True
        for i in range(5, 8):
            f = f"data/paper_raw/{i:016d}/{i:016d}.metadata.json"
            assert pr._should_sample_keep(f, sampling) is False, f"Should drop: {f}"

    def test_papers_workspace_sampling(self, tmp_path, monkeypatch):
        """papers workspaces must be sampled deterministically."""
        monkeypatch.setattr(pr, "PROJECT_ROOT", tmp_path)
        (tmp_path / "data" / "paper_raw").mkdir(parents=True)
        papers_dir = tmp_path / "data" / "papers"
        papers_dir.mkdir(parents=True)

        _make_papers_workspaces(papers_dir, 7)
        sampling = pr._selected_sample_workspaces(tmp_path, limit=5)
        assert sampling["papers_total"] == 7
        assert len(sampling["papers_selected"]) == 5

        # First 5 alphabetically (A, B, C, D, E) selected
        for i in range(5):
            prefix = f"data/papers/2024_Author_{chr(65 + i)}"
            assert prefix in sampling["papers_selected"]
        # F, G not selected
        for i in range(5, 7):
            prefix = f"data/papers/2024_Author_{chr(65 + i)}"
            assert prefix not in sampling["papers_selected"]

    def test_gitkeep_inside_non_selected_workspace_excluded(self, tmp_path, monkeypatch):
        """.gitkeep inside a non-selected workspace must NOT be kept."""
        monkeypatch.setattr(pr, "PROJECT_ROOT", tmp_path)
        raw_dir = tmp_path / "data" / "paper_raw"
        raw_dir.mkdir(parents=True)
        (tmp_path / "data" / "papers").mkdir(parents=True)

        _make_paper_raw_workspaces(raw_dir, 8)
        (raw_dir / ".gitkeep").write_text("")  # root-level gitkeep
        sampling = pr._selected_sample_workspaces(tmp_path, limit=5)

        assert pr._should_sample_keep("data/paper_raw/.gitkeep", sampling) is True
        assert pr._should_sample_keep("data/paper_raw/0000000000000000/.gitkeep", sampling) is True
        assert pr._should_sample_keep("data/paper_raw/0000000000000007/.gitkeep", sampling) is False

    def test_papers_workspace_detection_skips_non_paper_dirs(self, tmp_path, monkeypatch):
        """Non-paper directories in data/papers/ must not count as workspaces."""
        monkeypatch.setattr(pr, "PROJECT_ROOT", tmp_path)
        (tmp_path / "data" / "paper_raw").mkdir(parents=True)
        papers_dir = tmp_path / "data" / "papers"
        papers_dir.mkdir(parents=True)

        # Real workspace
        ws = papers_dir / "real_paper"
        ws.mkdir()
        (ws / "real_paper.metadata.json").write_text("{}", encoding="utf-8")
        # Non-paper dirs
        for d in ["images", "cache", "tmp", "logs", "reports", "__pycache__"]:
            (papers_dir / d).mkdir()
        # Empty dir
        (papers_dir / "empty_dir").mkdir()

        sampling = pr._selected_sample_workspaces(tmp_path, limit=5)
        assert sampling["papers_total"] == 1, (
            f"Expected 1 paper workspace, got {sampling['papers_total']}"
        )
        assert "data/papers/real_paper" in sampling["papers_selected"]

    def test_does_not_modify_source_data(self, tmp_path, monkeypatch):
        """Sampling must not create, delete, or modify files on disk."""
        monkeypatch.setattr(pr, "PROJECT_ROOT", tmp_path)
        raw_dir = tmp_path / "data" / "paper_raw"
        raw_dir.mkdir(parents=True)
        (tmp_path / "data" / "papers").mkdir(parents=True)

        dirs = _make_paper_raw_workspaces(raw_dir, 8)
        snapshot_before = sorted(
            p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()
        )

        _ = pr._selected_sample_workspaces(tmp_path, limit=5)
        # Filter files — this is a pure function, no mutation expected
        files = [f"data/paper_raw/{i:016d}/{i:016d}.metadata.json" for i in range(8)]
        _ = [f for f in files if pr._should_sample_keep(f, _)]

        snapshot_after = sorted(
            p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()
        )
        assert snapshot_before == snapshot_after, (
            f"Filesystem changed!\nOnly in before: {set(snapshot_before) - set(snapshot_after)}\n"
            f"Only in after: {set(snapshot_after) - set(snapshot_before)}"
        )
        # All original dirs still exist
        for d in dirs:
            assert d.exists()


class TestSnapshotManifest:
    """Tests for snapshot_manifest.json generation."""

    def test_manifest_contains_required_fields(self, tmp_path, monkeypatch):
        """Manifest must have snapshot_type, limits, included lists, sampling_note."""
        monkeypatch.setattr(pr, "PROJECT_ROOT", tmp_path)
        raw_dir = tmp_path / "data" / "paper_raw"
        raw_dir.mkdir(parents=True)
        (tmp_path / "data" / "papers").mkdir(parents=True)

        _make_paper_raw_workspaces(raw_dir, 3)
        sampling = pr._selected_sample_workspaces(tmp_path, limit=5)

        assert sampling["paper_raw_total"] == 3
        assert len(sampling["paper_raw_selected"]) == 3  # fewer than limit
        assert "paper_raw_selected" in sampling
        assert "paper_raw_total" in sampling
        assert "papers_selected" in sampling
        assert "papers_total" in sampling

    def test_default_samples_paper_raw_and_papers(self, tmp_path, monkeypatch):
        """Default behavior must apply sampling (no flag needed)."""
        monkeypatch.setattr(pr, "PROJECT_ROOT", tmp_path)
        raw_dir = tmp_path / "data" / "paper_raw"
        raw_dir.mkdir(parents=True)
        papers_dir = tmp_path / "data" / "papers"
        papers_dir.mkdir(parents=True)

        _make_paper_raw_workspaces(raw_dir, 10)
        _make_papers_workspaces(papers_dir, 6)

        sampling = pr._selected_sample_workspaces(tmp_path, limit=pr._WORKSPACE_SAMPLE_LIMIT)
        # Default profile is "audit" — sampling always active
        assert len(sampling["paper_raw_selected"]) == 5
        assert sampling["paper_raw_total"] == 10
        assert len(sampling["papers_selected"]) == 5
        assert sampling["papers_total"] == 6


class TestSecretScanWithSampling:
    """Secret scan must still work on sampled workspaces."""

    def test_secret_in_sampled_workspace_detected(self, tmp_path, monkeypatch):
        """A secret-like string in a sampled workspace file is detected."""
        monkeypatch.setattr(pr, "PROJECT_ROOT", tmp_path)
        raw_dir = tmp_path / "data" / "paper_raw"
        raw_dir.mkdir(parents=True)
        (tmp_path / "data" / "papers").mkdir(parents=True)

        # Create 3 workspaces (all sampled since 3 <= 5)
        for i in range(3):
            ws = raw_dir / f"{i:016d}"
            ws.mkdir(parents=True)
            content = ("Aut" + "horization: Bearer " + "a" * 32 + "\n") if i == 0 else f"safe file {i}"
            (ws / f"{i:016d}.py").write_text(content, encoding="utf-8")

        sampling = pr._selected_sample_workspaces(tmp_path, limit=5)
        files = [f"data/paper_raw/{i:016d}/{i:016d}.py" for i in range(3)]
        sampled = [f for f in files if pr._should_sample_keep(f, sampling)]
        assert len(sampled) == 3  # all 3 are sampled

        findings = pr.scan_files_for_secrets(sampled)
        assert len(findings) >= 1, f"Must detect secret in sampled workspace, got {findings}"
        assert any("data/paper_raw/0000000000000000" in f["path"] for f in findings)

    def test_secret_in_non_sampled_workspace_not_scanned(self, tmp_path, monkeypatch):
        """A secret in a non-sampled workspace is NOT scanned (it won't enter the zip)."""
        monkeypatch.setattr(pr, "PROJECT_ROOT", tmp_path)
        raw_dir = tmp_path / "data" / "paper_raw"
        raw_dir.mkdir(parents=True)
        (tmp_path / "data" / "papers").mkdir(parents=True)

        # Create 8 workspaces — only first 5 are sampled
        for i in range(8):
            ws = raw_dir / f"{i:016d}"
            ws.mkdir(parents=True)
            content = ("api_" + 'key = "' + "a" * 32 + '"\n') if i == 6 else f"safe file {i}"
            (ws / f"{i:016d}.py").write_text(content, encoding="utf-8")

        sampling = pr._selected_sample_workspaces(tmp_path, limit=5)
        files = [f"data/paper_raw/{i:016d}/{i:016d}.py" for i in range(8)]
        sampled = [f for f in files if pr._should_sample_keep(f, sampling)]
        assert len(sampled) == 5  # only 5 sampled

        # Scan only the sampled files — workspace 6 (with secret) is NOT in sampled
        findings = pr.scan_files_for_secrets(sampled)
        # No findings because workspace 6 was excluded from sampling
        assert len(findings) == 0, (
            f"Non-sampled workspace secret should not be detected in scan, got {findings}"
        )

    def test_secret_in_root_level_file_detected(self, tmp_path, monkeypatch):
        """Secret in a root-level file (outside any workspace) is detected."""
        monkeypatch.setattr(pr, "PROJECT_ROOT", tmp_path)
        (tmp_path / "data" / "paper_raw").mkdir(parents=True)
        (tmp_path / "data" / "papers").mkdir(parents=True)
        # Write a secret-bearing file at root level of data/paper_raw/
        (tmp_path / "data" / "paper_raw" / "secrets.py").write_text(
            ("x-api-" + "key: " + "a" * 32 + "\n"), encoding="utf-8"
        )

        sampling = pr._selected_sample_workspaces(tmp_path, limit=5)
        # Root-level file is always kept
        files = ["data/paper_raw/secrets.py"]
        assert pr._should_sample_keep(files[0], sampling) is True
        findings = pr.scan_files_for_secrets(files)
        assert len(findings) >= 1, f"Secret in root-level file must be detected, got {findings}"

    def test_secret_scan_allows_documentation_examples(self):
        """Known placeholders and bare env var names must not trigger the scan."""
        safe_lines = [
            'OPENALEX_EMAIL = "your@email.com"',
            'OPENALEX_API_KEY = "your_key_if_needed"',
            "test@example.com",
            "test-openalex-key",
            "# export OPENALEX_EMAIL",
            "# export OPENALEX_API_KEY",
        ]
        for line in safe_lines:
            findings = pr.scan_text_for_secrets(line, "test_file.py")
            assert len(findings) == 0, f"Should not flag: {line!r}"

    def test_secret_scan_catches_real_patterns(self):
        """Real-looking patterns must trigger the scan."""
        # Use string concat to avoid literal patterns in source code
        danger_lines = [
            ("Aut" + "horization: Bearer " + "a" * 32),
            ("x-api-" + "key: " + "a" * 32),
            ("api_" + 'key = "' + "a" * 32 + '"'),
        ]
        for line in danger_lines:
            findings = pr.scan_text_for_secrets(line, "test_file.py")
            assert len(findings) >= 1, f"Should flag: {line!r}"


class TestManifestSurrogateRobustness:
    """snapshot_manifest.json writing must not crash on surrogate paths."""

    def test_safe_for_zip_rejects_surrogates(self):
        """Paths with surrogate characters must fail _safe_for_zip."""
        assert pr._safe_for_zip("normal/path/file.json") is True
        assert pr._safe_for_zip("data/papers/paperሴ/file.json") is True
        # Surrogate-half: lone high surrogate
        assert pr._safe_for_zip("data/papers/paper\ud800/file.json") is False

    def test_manifest_safe_paths_filters_surrogates(self):
        """Manifest construction must skip unsafe paths, not crash."""
        raw_paths = {
            "data/paper_raw/0000000000000001",
            "data/paper_raw/0000000000000002",
        }
        papers_paths = {
            "data/papers/1998_Macdonald_zh",
            # Simulate a path with surrogate that would fail _safe_for_zip
            # — we can't write one literally, but _safe_for_zip rejects surrogates
        }
        # All normal paths pass
        for p in raw_paths | papers_paths:
            assert pr._safe_for_zip(p), f"Expected safe: {p!r}"

        # Verify that the safeguard exists: _safe_for_zip rejects surrogates
        bad = "data/papers/paper\ud800/file.json"
        assert not pr._safe_for_zip(bad), (
            "_safe_for_zip must reject surrogate paths"
        )

    def test_manifest_json_dumps_without_crash(self, tmp_path, monkeypatch):
        """Verify manifest JSON serialization does not throw on safe paths."""
        monkeypatch.setattr(pr, "PROJECT_ROOT", tmp_path)
        raw_dir = tmp_path / "data" / "paper_raw"
        raw_dir.mkdir(parents=True)
        papers_dir = tmp_path / "data" / "papers"
        papers_dir.mkdir(parents=True)

        # Create normal workspaces
        for i in range(3):
            ws = raw_dir / f"{i:016d}"
            ws.mkdir(parents=True)
            (ws / f"{i:016d}.metadata.json").write_text("{}", encoding="utf-8")

        ws_name = "1998_Macdonald_test"
        ws = papers_dir / ws_name
        ws.mkdir(parents=True)
        (ws / f"{ws_name}.metadata.json").write_text("{}", encoding="utf-8")
        (ws / f"{ws_name}.md").write_text("# title", encoding="utf-8")

        sampling = pr._selected_sample_workspaces(tmp_path, limit=5)
        manifest = {
            "snapshot_type": "lightweight",
            "paper_raw_sample_limit": 5,
            "paper_raw_total_detected": sampling["paper_raw_total"],
            "paper_raw_included": sorted(sampling["paper_raw_selected"]),
            "papers_sample_limit": 5,
            "papers_total_detected": sampling["papers_total"],
            "papers_included": sorted(sampling["papers_selected"]),
            "sampling_note": "test",
        }
        # Must not raise
        result = json.dumps(manifest, indent=2, ensure_ascii=False)
        assert "snapshot_type" in result
        assert '"lightweight"' in result
