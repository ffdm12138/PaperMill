"""Hygiene guard — AGENTS.md/CLAUDE.md must document keyword discovery concurrency."""

from pathlib import Path

import pytest


pytestmark = pytest.mark.hygiene

ROOT = Path(__file__).resolve().parents[2]


def test_agents_claude_are_byte_identical():
    """AGENTS.md and CLAUDE.md must be byte-identical (pre-existing contract)."""
    agents = (ROOT / "AGENTS.md").read_bytes()
    claude = (ROOT / "CLAUDE.md").read_bytes()
    assert agents == claude, "AGENTS.md and CLAUDE.md drifted apart"


def test_agents_docs_require_concurrent_keyword_discovery():
    """AGENTS.md and CLAUDE.md must contain the concurrency rule section."""
    for rel in ["AGENTS.md", "CLAUDE.md"]:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "Keyword metadata discovery concurrency rule" in text, f"{rel} missing section"
        assert ".paper_raw_write.lock" in text, f"{rel} missing lock ref"
        assert "discover_papers_concurrent.py" in text, f"{rel} missing wrapper script ref"
        assert "concurrent" in text.lower(), f"{rel} must mention concurrent"
        assert "allocator write critical section" in text, f"{rel} missing allocator section wording"
        assert "final duplicate guard" in text, f"{rel} missing final guard wording"


def test_agents_docs_do_not_require_serial_discovery():
    """AGENTS.md and CLAUDE.md must NOT force serial-only execution."""
    for rel in ["AGENTS.md", "CLAUDE.md"]:
        text = (ROOT / rel).read_text(encoding="utf-8")
        forbidden = [
            "must run one keyword at a time",
            "avoid concurrent discover_papers",
            "不要并发 discover_papers",
        ]
        for phrase in forbidden:
            assert phrase not in text.lower(), f"{rel} contains forbidden phrase: {phrase}"


def test_discover_papers_epilog_mentions_concurrent():
    """scripts/discover_papers.py must have an epilog mentioning concurrent option."""
    text = (ROOT / "scripts" / "discover_papers.py").read_text(encoding="utf-8")
    assert "discover_papers_concurrent.py" in text
    assert ".paper_raw_write.lock" in text


def test_discover_papers_concurrent_script_exists():
    """The concurrent wrapper script must exist."""
    assert (ROOT / "scripts" / "discover_papers_concurrent.py").exists()


def test_script_usage_docs_new_subsection():
    """docs/SCRIPT_USAGE.md must have a Discovery / Metadata discovery subsection."""
    text = (ROOT / "docs" / "SCRIPT_USAGE.md").read_text(encoding="utf-8")
    assert "Discovery / Metadata discovery" in text
    assert "discover_papers_concurrent.py" in text


def test_agents_docs_document_dual_lane_notebooks():
    """AGENTS.md and CLAUDE.md must document the Refresh/Backfill notebook model."""
    for rel in ["AGENTS.md", "CLAUDE.md"]:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "Dual-lane keyword notebooks" in text, f"{rel} missing dual-lane section"
        assert "Refresh" in text and "Backfill" in text, f"{rel} missing Refresh/Backfill"
        assert "keyword_notebooks" in text, f"{rel} missing notebook dir ref"
        assert "manage_discovery_keywords.py" in text, f"{rel} missing manage script ref"


def test_contract_documents_dual_lane():
    """docs/PROJECT_CONTRACT.md must document the dual-lane contract."""
    text = (ROOT / "docs" / "PROJECT_CONTRACT.md").read_text(encoding="utf-8")
    assert "关键词 discovery 双通道契约" in text
    assert "Refresh" in text and "Backfill" in text


def test_pack_repo_excludes_discovery_reports():
    """pack_repo.py must exclude data/discovery/reports/ from snapshot via _should_pack().

    Uses AST to find the _DATA_SKIP_DIRS assignment (a set assigned to a Name node)
    and verify "data/discovery/reports" is in it.
    """
    import ast

    source = (ROOT / "scripts" / "pack_repo.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_DATA_SKIP_DIRS":
                    if isinstance(node.value, ast.Set):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str) \
                                    and "data/discovery/reports" in elt.value:
                                found = True
                                break
    assert found, "data/discovery/reports not found in _DATA_SKIP_DIRS assignment"


def test_pack_repo_excludes_keyword_notebooks():
    """pack_repo.py must exclude data/discovery/keyword_notebooks/ (runtime progress)."""
    import ast

    source = (ROOT / "scripts" / "pack_repo.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_DATA_SKIP_DIRS":
                    if isinstance(node.value, ast.Set):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str) \
                                    and "data/discovery/keyword_notebooks" in elt.value:
                                found = True
                                break
    assert found, "data/discovery/keyword_notebooks not found in _DATA_SKIP_DIRS assignment"


def test_gitignore_excludes_keyword_notebooks():
    """.gitignore must exclude data/discovery/keyword_notebooks/ but allow the example."""
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/discovery/keyword_notebooks/**" in text
    assert "!data/discovery/queries/keywords.example.txt" in text


def test_manage_discovery_keywords_script_exists():
    """The keyword notebook management script must exist."""
    assert (ROOT / "scripts" / "manage_discovery_keywords.py").exists()


def test_keyword_notebook_module_exists():
    """The keyword notebook service module must exist."""
    assert (ROOT / "src" / "discovery" / "keyword_notebook.py").exists()
    assert (ROOT / "src" / "discovery" / "provider_models.py").exists()
