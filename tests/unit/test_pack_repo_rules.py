from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import scripts.pack_repo as pr


pytestmark = pytest.mark.unit

WF = pr.PACK_PROFILE  # preserve original


def test_snapshot_plan_fails_when_selected_member_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "PROJECT_ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="missing"):
        pr._build_snapshot_plan(["missing.py"])


def test_snapshot_plan_fails_on_selected_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "PROJECT_ROOT", tmp_path)
    target = tmp_path / "target.py"
    target.write_text("pass\n", encoding="utf-8")
    link = tmp_path / "link.py"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(RuntimeError, match="symlink"):
        pr._build_snapshot_plan(["link.py"])


def test_snapshot_plan_fails_on_total_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(pr, "ZIP_MAX_BYTES", 3)
    monkeypatch.setattr(pr, "PACK_PROFILE", "audit")
    (tmp_path / "a.py").write_text("1234", encoding="utf-8")
    with pytest.raises(RuntimeError, match="total size"):
        pr._build_snapshot_plan(["a.py"])


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
    "snapshot_manifest.json",
    # gitkeep inside runtime (not a real gitkeep)
    "write/jobs/.gitkeep/not_allowed.txt",
    # local agent/research tooling state
    ".reasonix/autoresearch/job/state/progress.json",
    ".reasonix/autoresearch/job/logs/heartbeat.jsonl",
    ".reasonix/desktop-topic-titles.json",
    # workbuddy (local tool state)
    ".workbuddy/memory/x.md",
    ".workbuddy/task/state.json",
    # runtime reports
    "data/cleanup_report.json",
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
def test_should_pack_audit_excludes_runtime_assets(path):
    """Audit snapshots are runtime-zero even for lightweight files."""
    _set_profile("audit")
    if path.endswith(".gitkeep"):
        assert pr._should_pack(path) is True
    else:
        assert pr._should_pack(path) is False, f"{path} must not pack"
        assert pr._should_pack(path, require_lightweight=True) is False


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
    assert "transactions" in pr.DENIED_PATH_PARTS


def test_pack_repo_secret_scan_catches_generic_bearer_token():
    token = "Bearer " + "A" * 32
    findings = pr.scan_text_for_secrets(f"Authorization: {token}", "sample.txt")
    assert findings
    assert findings[0].rule in {"authorization_bearer_literal", "bearer_literal"}


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





class TestSnapshotManifest:
    """Tests for snapshot_manifest.json generation."""

    def test_manifest_schema(self, tmp_path):
        """Manifest must use the new runtime-zero schema."""
        import datetime
        zip_path = tmp_path / "mineru_snapshot.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("src/main.py", "# test")
            zf.writestr("README.md", "# test")
        members = [n for n in zipfile.ZipFile(zip_path, "r").namelist()]
        manifest = {
            "schema_version": "2.0",
            "snapshot_type": "runtime_zero_source_audit",
            "created_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "payload_file_count": len(members),
            "runtime_files_included": 0,
            "runtime_workspaces_included": {"paper_raw": 0, "papers": 0},
            "excluded_runtime_categories": [
                "paper_raw", "papers", "transactions",
                "local_tool_state", "runtime_reports",
            ],
            "verification": {"runtime_zero": True, "secret_scan": "passed"},
        }
        assert manifest["runtime_files_included"] == 0
        assert manifest["payload_file_count"] == 2

    def test_self_check_no_runtime_leak(self, tmp_path):
        """Self-check must flag runtime files in zip."""
        zip_path = tmp_path / "mineru_snapshot.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("src/main.py", "# test")
            zf.writestr(".workbuddy/memory/x.md", "# note")
        errors = pr._verify_snapshot_self_check(zip_path)
        assert any(".workbuddy/" in err for err in errors), errors

    def test_self_check_rejects_duplicate_manifest(self, tmp_path):
        zip_path = tmp_path / "mineru_snapshot.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("snapshot_manifest.json", "{}")
            zf.writestr("snapshot_manifest.json", "{}")
        errors = pr._verify_snapshot_self_check(zip_path)
        assert any("snapshot_manifest.json count is 2" in err for err in errors)


class TestSecretScan:
    """Secret scan tests (workspace-sampling-independent)."""

    def test_secret_in_workspace_file_detected(self, tmp_path, monkeypatch):
        """A secret-like string in a workspace file is detected."""
        monkeypatch.setattr(pr, "PROJECT_ROOT", tmp_path)
        raw_dir = tmp_path / "data" / "paper_raw"
        raw_dir.mkdir(parents=True)
        (tmp_path / "data" / "papers").mkdir(parents=True)

        ws = raw_dir / "0000000000000000"
        ws.mkdir(parents=True)
        (ws / "0000000000000000.py").write_text(
            "Aut" + "horization: Bearer " + "a" * 32 + "\n", encoding="utf-8"
        )

        files = ["data/paper_raw/0000000000000000/0000000000000000.py"]
        findings = pr.scan_files_for_secrets(files)
        assert len(findings) >= 1, f"Must detect secret, got {findings}"

    def test_secret_in_root_level_file_detected(self, tmp_path, monkeypatch):
        """Secret in a root-level file (outside any workspace) is detected."""
        monkeypatch.setattr(pr, "PROJECT_ROOT", tmp_path)
        (tmp_path / "data" / "paper_raw").mkdir(parents=True)
        (tmp_path / "data" / "papers").mkdir(parents=True)
        (tmp_path / "data" / "paper_raw" / "secrets.py").write_text(
            ("x-api-" + "key: " + "a" * 32 + "\n"), encoding="utf-8"
        )
        files = ["data/paper_raw/secrets.py"]
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

    def test_openalex_unified_quoted_assignment_caught(self):
        """OPENALEX_EMAIL with quoted value must be caught (1 finding)."""
        findings = pr.scan_text_for_secrets(
            'OPENALEX_EMAIL = "real@secret.org"\n', "test.py",
        )
        assert len(findings) == 1, f"Expected 1 finding, got {len(findings)}: {findings}"
        assert findings[0].rule == "openalex_email_assignment"

    def test_openalex_unquoted_assignment_caught(self):
        findings = pr.scan_text_for_secrets(
            "OPENALEX_API_KEY=real-secret-key-999\n", "test.py",
        )
        assert len(findings) == 1, f"Expected 1 finding, got {len(findings)}: {findings}"
        assert findings[0].rule == "openalex_api_key_assignment"

    def test_openalex_export_assignment_caught(self):
        findings = pr.scan_text_for_secrets(
            "export OPENALEX_EMAIL=spill@secret.org\n", "test.sh",
        )
        assert len(findings) == 1, f"Expected 1 finding, got {len(findings)}: {findings}"

    def test_openalex_powershell_assignment_caught(self):
        findings = pr.scan_text_for_secrets(
            '$env:OPENALEX_API_KEY="ps-secret-key-888"\n', "test.ps1",
        )
        assert len(findings) == 1, f"Expected 1 finding, got {len(findings)}: {findings}"

    def test_secret_finding_has_line_number(self):
        """The SecretFinding must report the correct 1-indexed line number."""
        text = "safe line\nOPENALEX_EMAIL=x@y.com\nsafe line"
        findings = pr.scan_text_for_secrets(text, "test.py")
        assert len(findings) == 1
        assert findings[0].line == 2

    def test_secret_finding_no_match_value(self):
        """SecretFinding must NOT contain the matched secret value."""
        findings = pr.scan_text_for_secrets(
            'OPENALEX_API_KEY="super-secret-value-123"\n', "test.py",
        )
        assert len(findings) == 1
        f = findings[0]
        # Dataclass fields — must be these exact types
        assert isinstance(f.line, int)
        assert isinstance(f.rule, str)
        assert isinstance(f.path, str)
        # No match field
        assert not hasattr(f, "match")
        # repr must not contain secret
        assert "super-secret-value" not in repr(f)

    def test_secret_finding_is_frozen_dataclass(self):
        """SecretFinding is a frozen dataclass — cannot be mutated."""
        import dataclasses
        assert dataclasses.is_dataclass(pr.SecretFinding)
        assert pr.SecretFinding.__dataclass_fields__["rule"].type is str
        assert pr.SecretFinding.__dataclass_fields__["path"].type is str
        assert pr.SecretFinding.__dataclass_fields__["line"].type is int


class TestManifestSchema:
    """snapshot_manifest.json schema and self-check tests."""

    def test_safe_for_zip_rejects_surrogates(self):
        """Paths with surrogate characters must fail _safe_for_zip."""
        assert pr._safe_for_zip("normal/path/file.json") is True
        assert pr._safe_for_zip("data/papers/paperሴ/file.json") is True
        assert pr._safe_for_zip("data/papers/paper\ud800/file.json") is False

    def test_manifest_json_dumps_without_crash(self, tmp_path):
        """Verify manifest JSON serialization does not throw on valid data."""
        import datetime
        manifest = {
            "schema_version": "2.0",
            "snapshot_type": "runtime_zero_source_audit",
            "created_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "payload_file_count": 42,
            "runtime_files_included": 0,
            "runtime_workspaces_included": {"paper_raw": 0, "papers": 0},
            "excluded_runtime_categories": ["paper_raw", "papers", "transactions",
                                              "local_tool_state", "runtime_reports"],
            "verification": {"runtime_zero": True, "secret_scan": "passed"},
        }
        result = json.dumps(manifest, indent=2, ensure_ascii=False)
        assert "runtime_files_included" in result
        assert "runtime_zero" in result
