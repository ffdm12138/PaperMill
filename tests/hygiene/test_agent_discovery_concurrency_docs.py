"""Hygiene guard — AGENTS.md/CLAUDE.md must document keyword discovery concurrency."""

from pathlib import Path

import pytest


pytestmark = pytest.mark.hygiene

ROOT = Path(__file__).resolve().parents[2]



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
        assert "keyword notebook schema v4" in text, f"{rel} missing strict v4 contract"
        assert "keyword_zh" in text and "search_queries" in text
        assert "query text never becomes a Catalog category" in text
        assert "Active discovery never expands queries" in text


def test_contract_documents_dual_lane():
    """docs/PROJECT_CONTRACT.md must document the dual-lane contract."""
    text = (ROOT / "docs" / "PROJECT_CONTRACT.md").read_text(encoding="utf-8")
    assert "关键词 discovery 双通道契约" in text
    assert "Refresh" in text and "Backfill" in text





