"""Build a runtime-zero source audit snapshot ZIP.

This is a lightweight audit/handoff snapshot, NOT a source release tarball
or a full data backup.

Contract:
- Git repository maintains source-only hygiene: real paper assets, PDFs,
  images, runtime data, and logs must not be git-tracked.
- The snapshot includes all program source code, tests, docs, configs, and
  lightweight text/structure files (`.json` / `.md` / `.yaml` / `.toml` / `.csv`
  / `.bib` / `.tex` / `.py` / `.sh` / `.bat` etc.) from the workspace.
- ZIP **excludes**: PDFs, images, logs, caches, temp files, databases, model
  weights, secrets, large files, local tool state, runtime reports, and
  real paper workspaces (paper_raw / papers / transactions).
- **Runtime-zero**: no local tool state (`.workbuddy/`, `.reasonix/`),
  no runtime reports (`data/cleanup_report.json`), no live paper workspaces
  enter the snapshot regardless of profile.
- **Secret scan**: only scans files entering the snapshot ZIP, does not
  guarantee the full repo is secret-free.

Usage:
    python scripts/pack_repo.py                         # audit profile (default)
    python scripts/pack_repo.py --profile source        # git-tracked source only
    python scripts/pack_repo.py --name v2 --profile audit
"""
import argparse
from dataclasses import dataclass
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
import secrets
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from src.utils.repository_hygiene import (
    is_forbidden_snapshot_member,
    runtime_workspace_counts,
)
ZIP_NAME_BASE = "mineru_snapshot"
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")

# ── Packaging profiles ────────────────────────────────────────────
# audit (default): source code + untracked lightweight source/synthetic fixtures
# source:          only git-tracked source + .gitkeep
PACK_PROFILE = "audit"

# ── Lightweight audit snapshot: allowlist / denylist ──────────────
# LIGHTWEIGHT_ALLOWED_SUFFIXES: text/structure files allowed in zip
# (both git-tracked AND extra-scanned workspace files).
# HEAVY_OR_BINARY_DENIED_SUFFIXES: heavy/binary files NEVER allowed.
# DENIED_PATH_PARTS: directory components whose contents are NEVER allowed.
# DENIED_NAMES: specific filenames NEVER allowed (secrets).
#
# The extra workspace scan (audit profile) collects all files matching
# LIGHTWEIGHT_ALLOWED_SUFFIXES from across the entire project tree,
# then filters through DENIED_PATH_PARTS / DENIED_NAMES / DENIED_SUFFIXES.
# This ensures .json/.md/.txt/.yaml etc. from data/papers, data/paper_raw,
# data/catalog, write/jobs, and any other directory can enter the zip,
# while PDFs, images, logs, caches, secrets, binaries, and large files stay out.

LIGHTWEIGHT_ALLOWED_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".csv",
    ".tsv",
    ".bib",
    ".tex",
    ".sty",
    ".cls",
    ".sh",
    ".bat",
    ".ps1",
    ".html",
    ".css",
    ".js",
    ".gitkeep",
    ".paper.number",
}

HEAVY_OR_BINARY_DENIED_SUFFIXES = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".svgz",
    ".zip",
    ".tar",
    ".gz",
    ".7z",
    ".rar",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".tmp",
    ".bak",
    ".pkl",
    ".pickle",
    ".pt",
    ".pth",
    ".onnx",
    ".bin",
    ".npy",
    ".npz",
    ".parquet",
}

# Specific filenames NEVER allowed in zip regardless of profile.
DENIED_NAMES = {
    ".env",
    ".env.local",
    "credentials.json",
    "token.json",
    "secrets.json",
    "service-account.json",
    ".dockerignore",
    # Runtime reports
    "cleanup_report.json",
}

# Path components whose contents are NEVER allowed in zip.
DENIED_PATH_PARTS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    "node_modules",
    ".venv",
    "venv",
    "output",
    "images",
    "cache",
    "tmp",
    "temp",
    "logs",
    "transactions",
    ".local",
}

# Migration/operator state is deliberately excluded even when it lives
# outside the normal runtime roots.  Example/template/schema files and
# synthetic test fixtures are allowed so the contract itself can be tested.
MIGRATION_STATE_FILENAMES = frozenset({
    "mapping.json",
    "plan.json",
    "recovery-plan.json",
})
SAFE_ARTIFACT_MARKERS = frozenset({
    "example", "examples", "template", "templates",
    "schema", "schemas", "fixture", "fixtures",
})
_SAFE_ARTIFACT_NAME_RE = re.compile(
    r"(?:^|[._-])(example|examples|template|templates|schema|schemas)"
    r"(?:[._-]|$)",
    re.IGNORECASE,
)
_SNAPSHOT_TEXT_SUFFIXES = frozenset({
    ".json", ".jsonl", ".md", ".txt", ".yaml", ".yml", ".toml",
})
_SOURCE_SHA256_RE = re.compile(
    r'"source_sha256"\s*:\s*"([0-9a-fA-F]{64})"',
)
_CONFIRMED_STATUS_RE = re.compile(
    r'"status"\s*:\s*"confirmed"', re.IGNORECASE,
)
# Repair runtime filenames — historical catalog repair JSONs that contain
# real paper_name, author lists, and per-paper mapping decisions.  These
# are operator/historical artifacts, never part of the runtime-zero source.
_REPAIR_RUNTIME_RE = re.compile(
    r"repair_(?:final|mapping(?:_\w+)?|round\d+)\.json$",
    re.IGNORECASE,
)


def _is_example_or_fixture_path(rel_path: str) -> bool:
    """Return whether a path is an explicitly safe example/test fixture."""
    path = PurePosixPath(rel_path.replace("\\", "/"))
    parts = {part.casefold() for part in path.parts}
    if parts & SAFE_ARTIFACT_MARKERS:
        return True
    return bool(_SAFE_ARTIFACT_NAME_RE.search(path.name))


def _snapshot_artifact_exclusion_reason(rel_path: str) -> str | None:
    """Return a reason when a source snapshot member is operator state."""
    rel = rel_path.replace("\\", "/").strip("/")
    path = PurePosixPath(rel)
    name = path.name.casefold()
    parts = {part.casefold() for part in path.parts}

    if is_forbidden_snapshot_member(rel):
        return "runtime state"
    if rel.casefold().startswith("migrations/"):
        if name.endswith(".real.json"):
            return "real migration mapping/state"
        if "confirmed" in name:
            return "confirmed migration mapping/state"
    if _is_example_or_fixture_path(rel):
        return None
    if name in MIGRATION_STATE_FILENAMES:
        return "migration plan or mapping state"
    if "backup" in parts:
        return "transaction backup"
    if "page_journals" in parts or "receipts" in parts:
        return "discovery journal/receipt state"
    if _REPAIR_RUNTIME_RE.search(name):
        return "catalog repair runtime data"
    if "temp_authors.json".casefold() == name:
        return "catalog repair runtime data"
    return None


def _snapshot_sensitive_content_errors(rel_path: str, raw: bytes) -> list[str]:
    """Conservatively reject real migration state by content as well as name."""
    rel = rel_path.replace("\\", "/")
    path = PurePosixPath(rel)
    if _is_example_or_fixture_path(rel) or path.suffix.casefold() not in _SNAPSHOT_TEXT_SUFFIXES:
        return []
    high_risk = (
        rel.casefold().startswith("migrations/")
        or path.name.casefold() in MIGRATION_STATE_FILENAMES
        or "mapping" in path.name.casefold()
        or "journal" in path.name.casefold()
        or "receipt" in path.name.casefold()
    )
    if not high_risk:
        return []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return [f"unable to inspect sensitive migration member: {rel}"]

    errors: list[str] = []
    if _CONFIRMED_STATUS_RE.search(text) and (
        '"source_notebook"' in text or '"source_sha256"' in text
    ):
        errors.append(f"confirmed mapping present in snapshot: {rel}")
    for match in _SOURCE_SHA256_RE.finditer(text):
        value = match.group(1).lower()
        if len(set(value)) > 1:
            errors.append(f"real source SHA mapping present in snapshot: {rel}")
            break
    if '"transaction_id"' in text or '"backup_manifest_sha256"' in text:
        errors.append(f"migration transaction state present in snapshot: {rel}")
    if '"request_signature"' in text or '"cursor"' in text:
        errors.append(f"runtime cursor/journal state present in snapshot: {rel}")
    # Real catalog repair mapping patterns (non-example files).
    if not _is_example_or_fixture_path(rel):
        if '"old_paper_name"' in text and '"new_paper_name"' in text and '"apply"' in text:
            errors.append(f"real catalog repair mapping present in snapshot: {rel}")
    return errors

# ── Required root-level files ──────────────────────────────────────────
# These must be present in every snapshot ZIP regardless of profile.
REQUIRED_ROOT_FILES: set[str] = {
    "LICENSE",
    ".gitignore",
    ".gitattributes",
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
}
# Platform-specific additions (present in this repo).
if (PROJECT_ROOT / "NOTICE").exists():
    REQUIRED_ROOT_FILES.add("NOTICE")

# Size limits (oversized files are skipped with a warning).
SINGLE_FILE_MAX_BYTES = 50 * 1024 * 1024   # 50 MB
ZIP_MAX_BYTES = 600 * 1024 * 1024           # 600 MB

SECRET_PLACEHOLDERS = {
    "your@email.com",
    "your_key_if_needed",
    "test@example.com",
    "test-openalex-key",
    # Known test-fixture values — real-format but synthetic.
    "leak-check-key-99999",
    "fetch-test-key-abc",
    "err-leak-key-99999",
    "leaked-key-12345",
    "leak-check@test.org",
    "fetch@test.org",
    "err-leak@test.org",
    "secret@leak.com",
}

# Test fixture files that legitimately contain fake credential literals for
# unit-test purposes.  They are still scanned — individual placeholder values
# (above) suppress false positives — but files listed here never fail the
# packer's secret gate.
# Value-level allowlist — a finding whose matched line contains one of these
# exact substrings is a known test placeholder, not a leaked secret.
# Files are never skipped wholesale.
TEST_SECRET_PLACEHOLDER_VALUES: frozenset[str] = frozenset({
    "test-openalex-key",
    "test-openalex-email",
    "test@example.com",
    "fake-api-key-123",
    "test_api_key_placeholder",
    "not-a-real-key",
    "placeholder_key_12345",
    "test-secret-value",
})


@dataclass(frozen=True)
class SecretFinding:
    """A single secret-like literal found during snapshot scanning.

    Only metadata about the finding is stored — the matched value is never
    kept in this object, logged, or printed.
    """
    rule: str
    path: str
    line: int


@dataclass(frozen=True)
class SnapshotMember:
    path: str
    size_bytes: int
    sha256: str


def _member_digest(members: list[SnapshotMember]) -> str:
    payload = [
        {"path": member.path, "size_bytes": member.size_bytes, "sha256": member.sha256}
        for member in members
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _build_snapshot_plan(files: list[str]) -> list[SnapshotMember]:
    members: list[SnapshotMember] = []
    total = 0
    for rel in sorted(set(files)):
        src = PROJECT_ROOT / rel
        if not _safe_for_zip(rel):
            raise RuntimeError(f"unsafe selected snapshot path: {rel!r}")
        if src.is_symlink():
            raise RuntimeError(f"selected snapshot member is a symlink: {rel}")
        if not src.is_file():
            raise RuntimeError(f"selected snapshot member missing: {rel}")
        raw = src.read_bytes()
        size = len(raw)
        if PACK_PROFILE == "audit" and size > SINGLE_FILE_MAX_BYTES:
            raise RuntimeError(f"selected snapshot member exceeds single-file limit: {rel}")
        total += size
        if PACK_PROFILE == "audit" and total > ZIP_MAX_BYTES:
            raise RuntimeError("selected snapshot members exceed total size limit")
        members.append(SnapshotMember(rel, size, hashlib.sha256(raw).hexdigest()))
    return members


SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "openalex_email_assignment",
        re.compile(
            r"^[ \t]*(?:export[ \t]+|\$env:)?"
            r"OPENALEX_EMAIL[ \t]*=[ \t]*"
            r"[\"']?([^\"'\s#]+@[^\"'\s#]+)[\"']?",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        "openalex_api_key_assignment",
        re.compile(
            r"^[ \t]*(?:export[ \t]+|\$env:)?"
            r"OPENALEX_API_KEY[ \t]*=[ \t]*"
            r"[\"']?([A-Za-z0-9._\-]{10,})[\"']?",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    ("semantic_scholar_api_key_assignment", re.compile(r"SEMANTIC_SCHOLAR_API_KEY\s*=\s*[\"']([A-Za-z0-9_\-]{10,})[\"']", re.IGNORECASE)),
    ("authorization_bearer_literal", re.compile(r"Authorization\s*:\s*Bearer\s+([A-Za-z0-9._\-]{16,})", re.IGNORECASE)),
    ("bearer_literal", re.compile(r"Bearer\s+([A-Za-z0-9._\-]{24,})", re.IGNORECASE)),
    ("x_api_key_literal", re.compile(r"x-api-key\s*[:=]\s*[\"']?([A-Za-z0-9._\-]{16,})[\"']?", re.IGNORECASE)),
    ("wiley_tdm_token_literal", re.compile(r"Wiley-TDM-Client-Token\s*[:=]\s*[\"']?([A-Za-z0-9._\-]{12,})[\"']?", re.IGNORECASE)),
    ("elsevier_api_key_literal", re.compile(r"X-ELS-APIKey\s*[:=]\s*[\"']?([A-Za-z0-9._\-]{12,})[\"']?", re.IGNORECASE)),
    ("generic_api_key_assignment", re.compile(r"\bapi_key\s*=\s*[\"']([A-Za-z0-9._\-]{16,})[\"']", re.IGNORECASE)),
]


def scan_text_for_secrets(text: str, rel_path: str = "") -> list[SecretFinding]:
    """Return literal secret findings while allowing bare env-var names.

    The matched value is never stored in the finding — only the rule name,
    file path, and 1-indexed line number are returned.
    """
    findings: list[SecretFinding] = []
    for name, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(1) if match.groups() else match.group(0)
            if (
                value in SECRET_PLACEHOLDERS
                or value in TEST_SECRET_PLACEHOLDER_VALUES
                or value.startswith("your_")
            ):
                continue
            line = text[:match.start()].count("\n") + 1
            findings.append(SecretFinding(
                rule=name,
                path=rel_path,
                line=line,
            ))
    return findings


def scan_files_for_secrets(
    files: list[str], *, include_tests: bool = False,
) -> list[SecretFinding]:
    """Scan files for hardcoded credential-like patterns.

    By default test files (``tests/``) are excluded — they legitimately
    contain fake credentials.  The authoritative packer entry point always
    passes *include_tests=True* so the snapshot scan covers every file that
    will actually ship.
    """
    findings: list[SecretFinding] = []
    for rel in files:
        if rel.startswith("tests/") and not include_tests:
            continue
        path = PROJECT_ROOT / rel
        if not _lightweight_suffix_match(rel, path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        findings.extend(scan_text_for_secrets(text, rel))
    return findings


def _lightweight_suffix_match(rel: str, path: Path) -> bool:
    """True if rel/path has a lightweight allow-listed suffix.

    Python's ``Path.suffix`` treats dotfiles like ``.gitkeep`` as having no
    suffix (``Path("a/.gitkeep").suffix == ""``) and multi-dot names like
    ``0000000000000001.paper.number`` as suffix ``.number`` (not
    ``.paper.number``). So a bare ``path.suffix`` membership check misses
    ``.gitkeep`` and ``.paper.number`` markers. Handle those two edge cases
    explicitly; everything else falls through to the standard suffix set.
    """
    if path.name == ".gitkeep" or rel.endswith(".gitkeep"):
        return True
    if rel.endswith(".paper.number"):
        return True
    return path.suffix.lower() in LIGHTWEIGHT_ALLOWED_SUFFIXES


def _heavy_suffix_match(path: Path) -> bool:
    """Like _lightweight_suffix_match, but for the heavy/binary deny set.

    ``.paper.number`` is not heavy; guard against the same multi-dot suffix
    quirk以防 a future heavy suffix ending in ``.number`` mis-firing.
    """
    return path.suffix.lower() in HEAVY_OR_BINARY_DENIED_SUFFIXES


def _safe_for_zip(rel_path: str) -> bool:
    """路径能否安全写入 zip（不含 surrogate 且可 UTF-8 编码）。"""
    if _SURROGATE_RE.search(rel_path):
        return False
    try:
        rel_path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    try:
        zipfile.ZipInfo(rel_path)
    except Exception:
        return False
    return True


def _is_local_backup_artifact(path: Path) -> bool:
    name = path.name.lower()
    return (
        name.endswith(".bak")
        or ".bak." in name
        or name.endswith(".backup")
        or ".backup." in name
        or name.endswith(".tmp")
        or name.endswith(".temp")
        or ".bak_" in name
    )


def _should_pack(rel_path: str, *, require_lightweight: bool = False) -> bool:
    """Universal deny rules + optional lightweight suffix check.

    ``require_lightweight=False`` (default): for git-tracked files.
        Only universal denies apply — a git-tracked ``Dockerfile``
        (suffixless) passes through.

    ``require_lightweight=True``: for the extra workspace scan
        (audit profile).  The file MUST have a suffix in
        ``LIGHTWEIGHT_ALLOWED_SUFFIXES`` AND pass all universal denies.
    """
    path = Path(rel_path)
    rel = rel_path.replace("\\", "/")

    # Exclude operator-controlled migration state before any suffix or
    # profile-specific handling.  This also protects audit's untracked scan.
    if _snapshot_artifact_exclusion_reason(rel):
        return False

    # 1. Apply the canonical repository runtime-zero policy.
    if is_forbidden_snapshot_member(rel):
        return False
    # 2. Skip test migration tombstone files.
    if rel.endswith("._deleted") or rel.endswith(".py._deleted"):
        return False
    # 3. Skip root scratch files (shell redirection accidents).
    _ROOT_SCRATCH = {"=", "keep_rank"}
    if rel in _ROOT_SCRATCH:
        return False
    if rel == "snapshot_manifest.json":
        return False
    # 4. Skip local backup / temporary artifacts.
    if _is_local_backup_artifact(path):
        return False
    # 5. Skip HEAVY_OR_BINARY_DENIED_SUFFIXES regardless of origin.
    if _heavy_suffix_match(path):
        return False
    # 6. Skip import/ directory (external-source PDFs).
    if path.parts and path.parts[0] == "import":
        return False

    # 7. Skip DENIED_PATH_PARTS — any component match blocks the file.
    for denied_part in DENIED_PATH_PARTS:
        if denied_part in path.parts:
            return False

    # 8. Skip DENIED_NAMES.
    if path.name in DENIED_NAMES:
        return False

    # 9. Skip write/ workspace (only .gitkeep / README.md).
    if path.parts and path.parts[0] == "write":
        _WRITE_KEEP = {"write/README.md", "write/.gitkeep", "write/jobs/.gitkeep"}
        if rel not in _WRITE_KEEP:
            return False

    # 11. Skip data/locks/*.lock files.
    if rel.startswith("data/locks/") and path.suffix == ".lock":
        return False

    # 12. Only allow reports/.gitkeep and template files.
    if rel.startswith("reports/"):
        if rel == "reports/.gitkeep" or "template" in path.name.lower():
            return True
        return False

    # 13. Catalog folders, links, assignments and classifier state are runtime.
    if rel.startswith("data/catalog/"):
        return rel == "data/catalog/.gitkeep"

    # For extra-scanned (non-git) files: must be lightweight text.
    if require_lightweight:
        return _lightweight_suffix_match(rel, path)

    return _safe_for_zip(rel_path)


def git_tracked_files(profile: str = "audit") -> list[str]:
    """Return sorted list of files (relative paths) for the snapshot.

    1. All git-tracked files filtered through ``_should_pack(require_lightweight=False)``.
    2. **Audit profile only**: extra workspace-wide scan for lightweight
       text/structure files (``.json`` / ``.md`` / ``.yaml`` / etc.) that
       pass ``_should_pack(require_lightweight=True)``.
       Runtime directories remain excluded by ``_should_pack``.  This captures
       dirty/untracked source and synthetic fixtures without leaking live data.

    For the ``source`` profile, a valid git repository is REQUIRED.
    If not available, the function prints an error and returns an empty list
    (the caller exits non-zero).
    """
    try:
        # Only --cached (tracked files). Untracked-but-not-ignored files are
        # NOT included here: source profile must be git-tracked only, and the
        # audit profile's workspace scan below already picks up lightweight
        # untracked/gitignored files as a superset of --others --exclude-standard.
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "ls-files", "--cached", "-z"],
            capture_output=True, text=True, encoding="utf-8",
            check=False,
        )
        if result.returncode == 0:
            files = [
                f.replace("\\", "/")
                for f in result.stdout.split("\0")
                if f and (PROJECT_ROOT / f).exists()
            ]
            if files:
                safe = [f for f in files if _should_pack(f)]
                skipped = [f for f in files if not _should_pack(f)]
                for f in skipped:
                    print(f"  [SKIP] excluded from snapshot: {f!r}")

                # Extra (audit only): workspace-wide scan for lightweight
                # text/structure files (catalog, metadata, markdown, configs,
                # source_records, etc.) even if git-ignored.
                safe_set = set(safe)
                if profile == "audit":
                    extras: list[str] = []
                    for path in sorted(PROJECT_ROOT.rglob("*")):
                        if not path.is_file():
                            continue
                        rel = path.relative_to(PROJECT_ROOT).as_posix()
                        if _should_pack(rel, require_lightweight=True):
                            extras.append(rel)
                    if extras:
                        new_count = sum(1 for e in extras if e not in safe_set)
                        safe = sorted(safe_set | set(extras))
                        if new_count:
                            print(f"  Added {new_count} lightweight file(s) from workspace")

                # Always inject REQUIRED_ROOT_FILES that exist on disk.
                for name in sorted(REQUIRED_ROOT_FILES):
                    if (PROJECT_ROOT / name).exists() and name not in safe_set:
                        safe.append(name)
                        safe_set.add(name)
                        print(f"  [INJECT] required root file: {name}")

                print(f"  Found {len(safe)} total files")
                return safe
        else:
            print(f"[WARN] git ls-files failed: {result.stderr}")
    except Exception as e:
        print(f"[WARN] git ls-files unavailable: {e}")

    # Source profile requires git; fail closed instead of falling back.
    if profile == "source":
        print("[ERROR] source profile requires a git repository; aborting")
        sys.exit(1)

    files = _scan_repo_files()
    print(f"  Found {len(files)} files from filesystem scan")
    return files


def _scan_repo_files() -> list[str]:
    """Fallback for zip snapshots without .git metadata.

    Scans entire workspace and filters through
    ``_should_pack(require_lightweight=True)`` so that lightweight
    text/structure files (including those in git-ignored runtime dirs)
    are included.
    """
    out: list[str] = []
    for path in sorted(PROJECT_ROOT.rglob("*")):
        if not path.is_file():
            continue
        if path.name.startswith("mineru_snapshot") and path.suffix == ".zip":
            continue
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if not _should_pack(rel, require_lightweight=True):
            continue
        out.append(rel)
    # Always inject REQUIRED_ROOT_FILES that exist on disk.
    for name in sorted(REQUIRED_ROOT_FILES):
        if (PROJECT_ROOT / name).exists() and name not in out:
            out.append(name)
    return sorted(out)


# -- post-pack self-check ---------------------------------------------------


def _verify_snapshot_self_check(zip_path: Path) -> list[str]:
    """Post-pack self-check: enforce the runtime-zero snapshot contract."""
    errors: list[str] = []
    manifest_count = 0
    manifest = None
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        payload = [name for name in names if name != "snapshot_manifest.json"]
        runtime = [name for name in payload if is_forbidden_snapshot_member(name)]
        workspaces = runtime_workspace_counts(payload)

        # 1. Required root-level files
        missing_required = REQUIRED_ROOT_FILES - set(names)
        for missing in sorted(missing_required):
            errors.append(f"required root file missing: {missing}")

        # 2. ZIP member safety
        for name in names:
            if name.startswith("/") or (len(name) > 2 and name[1] == ":"):
                errors.append(f"absolute path in zip: {name!r}")
            if ".." in name.split("/"):
                errors.append(f"path escape in zip: {name!r}")
            if names.count(name) > 1:
                errors.append(f"duplicate member in zip: {name!r}")
            try:
                info = zf.getinfo(name)
                if info.flag_bits & 0x8000:
                    errors.append(f"symlink member in zip: {name!r}")
            except Exception:
                pass

            if name == "snapshot_manifest.json":
                manifest_count += 1
                try: manifest = json.loads(zf.read(name).decode("utf-8"))
                except Exception as exc: errors.append(f"invalid snapshot manifest: {exc}")
                continue
            if name in runtime:
                errors.append(f"runtime file present in snapshot: {name}")
            reason = _snapshot_artifact_exclusion_reason(name)
            if reason:
                errors.append(f"sensitive snapshot member present ({reason}): {name}")
            try:
                errors.extend(_snapshot_sensitive_content_errors(name, zf.read(name)))
            except KeyError:
                errors.append(f"snapshot member disappeared during self-check: {name}")
    if manifest_count != 1:
        errors.append(f"snapshot_manifest.json count is {manifest_count}, expected 1")
    if not manifest_count:
        errors.append("snapshot_manifest.json missing from zip")
    if manifest is not None:
        expected = {
            "schema_version": "2.0",
            "snapshot_type": "runtime_zero_source_audit",
            "payload_file_count": len(payload),
            "runtime_files_included": len(runtime),
            "runtime_workspaces_included": workspaces,
        }
        for key, value in expected.items():
            if manifest.get(key) != value:
                errors.append(f"snapshot manifest {key} mismatch: {manifest.get(key)!r} != {value!r}")
        declared_members = manifest.get("members") or []
        actual_members: list[SnapshotMember] = []
        with zipfile.ZipFile(zip_path, "r") as verify_zf:
            for name in payload:
                raw = verify_zf.read(name)
                actual_members.append(SnapshotMember(name, len(raw), hashlib.sha256(raw).hexdigest()))
        actual_members.sort(key=lambda member: member.path)
        if declared_members != [member.__dict__ for member in actual_members]:
            errors.append("snapshot manifest members mismatch")
        selection = manifest.get("selection") or {}
        archive = manifest.get("archive") or {}
        digest = _member_digest(actual_members)
        for label, section in (("selection", selection), ("archive", archive)):
            if section.get("count") != len(actual_members) or section.get("sha256") != digest:
                errors.append(f"snapshot manifest {label} mismatch")
        if manifest.get("omissions") != []:
            errors.append("snapshot manifest omissions is not empty")
        verification = manifest.get("verification") or {}
        for key, value in {"runtime_zero": not runtime, "secret_scan": "passed", "archive_member_check": "passed"}.items():
            if verification.get(key) != value:
                errors.append(f"snapshot manifest verification.{key} mismatch")
    # ── import-closure check ──
    _SRC_IMPORT_RE = re.compile(
        r"^\s*from\s+(src\.[a-z_][a-z0-9_.]*)\s+import|^\s*import\s+(src\.[a-z_][a-z0-9_.]*)",
        re.MULTILINE,
    )
    with zipfile.ZipFile(zip_path, "r") as zf:
        zip_py_files: set[str] = {
            name for name in payload if name.endswith(".py")
        }
        zip_modules: set[str] = {
            name[:-3].replace("/", ".").replace("\\", ".")
            for name in zip_py_files
        }
        for name in sorted(zip_py_files):
            try:
                source = zf.read(name).decode("utf-8")
            except Exception:
                continue
            for match in _SRC_IMPORT_RE.finditer(source):
                imported = match.group(1) or match.group(2)
                if imported is None:
                    continue
                # Convert module path to expected ZIP path
                expected_zip = imported.replace(".", "/") + ".py"
                if expected_zip not in zip_py_files:
                    # Check if it's a package __init__.py
                    expected_init = imported.replace(".", "/") + "/__init__.py"
                    if expected_init not in zip_py_files:
                        errors.append(
                            f"missing import target: {name} imports {imported} "
                            f"but {expected_zip} is not in snapshot"
                        )
    return errors


def main():
    parser = argparse.ArgumentParser(description="Build runtime-zero source audit snapshot ZIP")
    parser.add_argument(
        "--name", type=str, default="",
        help="Suffix for zip filename, e.g. v2 -> mineru_snapshot_v2.zip",
    )
    parser.add_argument(
        "--profile", type=str, default="audit", choices=["audit", "source"],
        help='Packaging profile: "audit" includes dirty lightweight source and synthetic fixtures; '
             '"source" includes git-tracked source. Both exclude runtime data.',
    )
    args = parser.parse_args()

    global PACK_PROFILE
    PACK_PROFILE = args.profile

    suffix = args.name
    zip_name = f"{ZIP_NAME_BASE}_{suffix}.zip" if suffix else f"{ZIP_NAME_BASE}.zip"
    zip_path = PROJECT_ROOT / zip_name
    temp_zip = zip_path.with_name(f"{zip_path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")

    if PACK_PROFILE == "audit":
        print(f"  Profile: audit (runtime-zero source + synthetic fixtures)")
    else:
        print(f"  Profile: source (git-tracked only)")

    files = git_tracked_files(profile=PACK_PROFILE)
    if not files:
        print("[ERROR] No tracked files, aborting")
        sys.exit(1)

    # Pre-flight: required root files that exist on disk must be in the pack.
    _missing_root = [
        name for name in sorted(REQUIRED_ROOT_FILES)
        if (PROJECT_ROOT / name).exists() and name not in files
    ]
    if _missing_root:
        print(f"[ERROR] Required root files missing from pack list: {_missing_root}")
        sys.exit(1)

    # Secret scan on filtered list (only files entering the zip).
    secret_findings = scan_files_for_secrets(files, include_tests=True)
    if secret_findings:
        print("[ERROR] Secret-like literal(s) found; refusing to pack")
        for finding in secret_findings[:20]:
            print(f"  {finding.path}: {finding.rule} (line {finding.line})")
        if len(secret_findings) > 20:
            print(f"  ... {len(secret_findings) - 20} more")
        sys.exit(1)

    try:
        selected_members = _build_snapshot_plan(files)
    except Exception as exc:
        print(f"[ERROR] Snapshot selection failed: {exc}")
        sys.exit(1)
    count = 0

    try:
      with zipfile.ZipFile(temp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        archived_members: list[SnapshotMember] = []
        for member in selected_members:
            src = PROJECT_ROOT / member.path
            if src.is_symlink() or not src.is_file():
                raise RuntimeError(f"selected snapshot member changed before archive: {member.path}")
            raw = src.read_bytes()
            current = SnapshotMember(member.path, len(raw), hashlib.sha256(raw).hexdigest())
            if current != member:
                raise RuntimeError(f"selected snapshot member changed before archive: {member.path}")
            zf.writestr(member.path, raw)
            archived_members.append(current)
            count += 1
        if archived_members != selected_members:
            raise RuntimeError("snapshot archive omitted selected members")

        # Write snapshot manifest as last entry in the same session.
        # Build manifest from the actual ZIP members written so far.
        written_members = [name for name in zf.namelist() if name != "snapshot_manifest.json"]
        runtime_files_in_zip = [m for m in written_members if is_forbidden_snapshot_member(m)]
        workspace_counts = runtime_workspace_counts(written_members)
        synthetic_files = [m for m in written_members if m.startswith("tests/fixtures/synthetic_library/")]
        manifest = {
            "schema_version": "2.0",
            "snapshot_type": "runtime_zero_source_audit",
            "created_at": None,  # filled below
            "payload_file_count": len(written_members),
            "runtime_files_included": len(runtime_files_in_zip),
            "runtime_workspaces_included": {
                **workspace_counts,
            },
            "synthetic_fixtures_included": len(synthetic_files),
            "selection": {"count": len(selected_members), "sha256": _member_digest(selected_members)},
            "archive": {"count": len(archived_members), "sha256": _member_digest(archived_members)},
            "omissions": [],
            "members": [member.__dict__ for member in selected_members],
            "excluded_runtime_categories": [
                "paper_raw",
                "papers",
                "transactions",
                "local_tool_state",
                "runtime_reports",
            ],
            "verification": {
                "runtime_zero": len(runtime_files_in_zip) == 0,
                "secret_scan": "passed",
                "archive_member_check": "passed",
            },
        }
        import datetime
        manifest["created_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        zf.writestr("snapshot_manifest.json",
                    json.dumps(manifest, indent=2, ensure_ascii=False))
        count += 1  # manifest counts toward total

    # Post-pack self-check
      check_errors = _verify_snapshot_self_check(temp_zip)
      if check_errors:
        print("[ERROR] Snapshot self-check failed:")
        for e in check_errors:
            print(f"  {e}")
        sys.exit(1)
      os.replace(temp_zip, zip_path)
    finally:
      if temp_zip.exists():
        temp_zip.unlink()

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"[OK] Packed: {zip_name} ({count} files, {size_mb:.1f} MB)")
    print(f"     {zip_path}")


if __name__ == "__main__":
    main()
