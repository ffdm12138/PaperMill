from __future__ import annotations

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
