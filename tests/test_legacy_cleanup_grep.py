"""Guard against reintroducing v1 legacy concepts into the v2-only codebase.

Scans source/doc directories (excluding tests/ and __pycache__) for forbidden
v1 tokens. ``paper.md`` is allowed only inside scripts/validate_v2_library.py,
whose job is to detect and reject it.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

FORBIDDEN_TOKENS = [
    "papers_pdf",
    "register_manual_pdf",
    "import_pending_pdf",
    "library_index",
    "identity_index",
    "domain_catalog",
    "domain_library",
    "literature_catalog",
    "ai_summary",
    "relevance_to_my_work",
]

LEGACY_ONLY_ALLOWED = {
    "scripts/legacy/fix_paper_ids_batch.py",
    "scripts/legacy/fix_paperid_case.py",
    "scripts/legacy/fix_remaining_rename.py",
    "scripts/legacy/ingest_ids.py",
    "scripts/legacy/audit_paper_raw_formal_imports.py",
    "scripts/legacy/migrate_paper_raw_6digit_to_paper_number.py",
    "scripts/legacy/rename_english_papers.py",
    "scripts/legacy/rename_raw_to_paperid.py",
}

SCAN_DIRS = ["src", "scripts", "config", "web", "skills", "docs"]


def _source_files() -> list[Path]:
    out: list[Path] = []
    for sub in SCAN_DIRS:
        base = REPO / sub
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            if "__pycache__" in p.parts:
                continue
            if p.suffix in {".pyc", ".png", ".jpg", ".jpeg", ".pdf", ".zip"}:
                continue
            out.append(p)
    return out


def test_no_forbidden_legacy_tokens():
    offenders: list[str] = []
    for path in _source_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for token in FORBIDDEN_TOKENS:
            if token in text:
                offenders.append(f"{path.relative_to(REPO)}: {token}")
        if "legacy-only" in text:
            rel = str(path.relative_to(REPO)).replace("\\", "/")
            if rel not in LEGACY_ONLY_ALLOWED and "LEGACY MIGRATION SCRIPT" not in text:
                offenders.append(f"{rel}: legacy-only")
    assert not offenders, "forbidden v1 tokens found:\n" + "\n".join(offenders)


def test_normal_ingest_no_planned_source_id_or_six_digit_allocator():
    offenders: list[str] = []
    allowed = {
        "tests/test_legacy_migrate_paper_raw_6digit.py",
        "tests/test_legacy_cleanup_grep.py",
    }
    forbidden = ("planned_source_id", ":06d}", "_TEMP_ID_RE.match(p.name)")
    for sub in ("src", "scripts", "tests"):
        for path in (REPO / sub).rglob("*.py"):
            rel = str(path.relative_to(REPO)).replace("\\", "/")
            if rel in allowed or "__pycache__" in path.parts:
                continue
            if rel.startswith("scripts/legacy/"):
                continue
            text = path.read_text(encoding="utf-8")
            if "LEGACY MIGRATION SCRIPT" in text:
                continue
            for token in forbidden:
                if token in text:
                    offenders.append(f"{rel}: {token}")
    assert not offenders, "normal ingest still references legacy source-id allocation:\n" + "\n".join(offenders)


def test_paper_md_not_a_formal_path():
    """paper.md must not appear as a formal asset path in src/ or scripts/
    (except scripts/validate_v2_library.py, which guards against it)."""
    offenders: list[str] = []
    for path in _source_files():
        if "tests" in path.parts:
            continue
        rel = str(path.relative_to(REPO)).replace("\\", "/")
        if rel == "scripts/validate_v2_library.py":
            continue  # legitimately detects/rejects paper.md
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "paper.md" in text:
            offenders.append(rel)
    assert not offenders, "paper.md referenced as a path in:\n" + "\n".join(offenders)


def test_writer_does_not_read_legacy_citation_field():
    """src/writer and src/bib must not read a flat 'citation' field on catalog entries."""
    offenders: list[str] = []
    for sub in ("src/writer", "src/bib.py"):
        base = REPO / sub
        paths = [base] if base.is_file() else list(base.rglob("*.py"))
        for path in paths:
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if '"citation"' in text or "'citation'" in text or ".get(\"citation\"" in text:
                offenders.append(str(path.relative_to(REPO)))
    assert not offenders, "legacy citation field still read in:\n" + "\n".join(offenders)


def test_normal_tests_do_not_handwrite_ready_for_commit_artifacts():
    allowed = {
        "tests/helpers/paper_raw_factory.py",
        "tests/test_catalog_metadata_separation.py",
        "tests/test_legacy_paper_raw_formal_import_audit.py",
        "tests/test_metadata_quality_audit.py",
        "tests/test_repair_bad_formal_imports.py",
        "tests/test_paper_raw_commit_atomic.py",
        "tests/test_v2_library.py",
        # malformed _ready_dirs gate negative case
        "tests/test_manual_import_metadata_requirements.py",
    }
    tokens = ("ready_for_commit", ".paper.number", "formalization.json")
    write_ops = (".write_text(", "atomic_write_json(", "_write_json(")
    offenders: list[str] = []
    for path in (REPO / "tests").rglob("*.py"):
        rel = str(path.relative_to(REPO)).replace("\\", "/")
        if rel in allowed or "__pycache__" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if any(token in line for token in tokens) and any(op in line for op in write_ops):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, "normal tests handwrite formalize artifacts:\n" + "\n".join(offenders)


def test_formalize_main_path_does_not_call_legacy_repoint():
    offenders: list[str] = []
    for sub in ("src", "scripts", "tests"):
        base = REPO / sub
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            rel = str(path.relative_to(REPO)).replace("\\", "/")
            if rel == "tests/test_legacy_cleanup_grep.py":
                continue
            text = path.read_text(encoding="utf-8")
            legacy_repoint = "ledger." + "repoint("
            self_legacy_repoint = "self.ledger." + "repoint("
            if legacy_repoint in text or self_legacy_repoint in text:
                offenders.append(rel)
    assert not offenders, "legacy ledger repoint call remains:\n" + "\n".join(offenders)


def test_readonly_snapshot_docstring_has_no_temporary_aside():
    haystack = "\n".join(
        path.read_text(encoding="utf-8")
        for sub in ("src", "scripts", "tests", "docs", "skills")
        for path in (REPO / sub).rglob("*")
        if path.is_file()
        and str(path.relative_to(REPO)).replace("\\", "/") != "tests/test_legacy_cleanup_grep.py"
        and path.suffix not in {".pyc", ".png", ".jpg", ".jpeg", ".pdf", ".zip"}
    )
    assert "wait, it doesn't" not in haystack
    assert "wait, it does" not in haystack


def test_markdown_front_matter_rule_stays_first_100_lines():
    paths = [p for sub in ("src", "scripts", "tests", "docs", "skills") for p in (REPO / sub).rglob("*") if p.is_file()]
    haystack = "\n".join(
        path.read_text(encoding="utf-8")
        for path in paths
        if str(path.relative_to(REPO)).replace("\\", "/") != "tests/test_legacy_cleanup_grep.py"
        and path.suffix not in {".pyc", ".png", ".jpg", ".jpeg", ".pdf", ".zip"}
    )
    assert "first 10 lines" not in haystack
    assert "前 10 行" not in haystack
    assert "first 100" in haystack or "前 100" in haystack or "max_lines=100" in haystack
