"""Contract tests: ingest path ordering in docs — manual PDF = convert before resolve."""
from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.contract

_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_manual_pdf_path_is_convert_before_resolve():
    """README, AGENTS, ARCHITECTURE must show manual PDF path as convert-then-resolve."""
    for rel in ["README.md", "AGENTS.md", "docs/ARCHITECTURE.md"]:
        text = _read(rel)
        tl = text.lower()
        # must describe a manual PDF path
        assert (
            "manual pdf" in tl or "手动 pdf" in tl or "先转换" in text
        ), f"{rel} must mention manual PDF path"
        assert "convert" in tl, f"{rel} must mention convert"
        assert "resolve" in tl, f"{rel} must mention resolve"
        assert (
            "converted markdown" in tl or "转换后的 markdown" in tl or "转换完成后的 md" in text
        ), f"{rel} must mention converted Markdown as resolver input"
        assert "--move --apply" in text, f"{rel} must recommend move/apply for manual PDF staging"
        assert (
            "data/raw/` is a queue" in text
            or "data/raw/` 是待处理队列" in text
            or "raw 是待处理队列" in text
            or "manual PDF queue" in text
        ), f"{rel} must describe data/raw as a queue"

        # In command-chain / code blocks: resolve_paper_raw_metadata must appear
        # AFTER formal MinerU conversion, never before.
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if "resolve_paper_raw_metadata" in line:
                # check that this resolve line does NOT appear before a convert line
                # in the same block (same block = lines between blank lines)
                block_start = i
                while block_start > 0 and lines[block_start - 1].strip():
                    block_start -= 1
                block_end = i
                while block_end < len(lines) - 1 and lines[block_end + 1].strip():
                    block_end += 1
                block_lines = lines[block_start : block_end + 1]
                block_text = "\n".join(block_lines)
                if (
                    "convert_paper_raw_gpu" in block_text
                    and "resolve_paper_raw_metadata" in block_text
                ):
                    # within this block, convert must appear before resolve
                    convert_pos = next(
                        j for j, l in enumerate(block_lines) if "convert_paper_raw_gpu" in l
                    )
                    resolve_pos = next(
                        j for j, l in enumerate(block_lines) if "resolve_paper_raw_metadata" in l
                    )
                    assert resolve_pos > convert_pos, (
                        f"{rel}: in block starting at line {block_start + 1},"
                        f" resolve_paper_raw_metadata (line {i + 1}) must appear after"
                        f" convert_paper_raw_gpu (line {block_start + convert_pos + 1})"
                    )


def test_network_path_metadata_first():
    """README and AGENTS must describe the network metadata path: metadata/DOI before convert."""
    for rel in ["README.md", "AGENTS.md"]:
        text = _read(rel)
        tl = text.lower()
        assert (
            "network" in tl and "metadata" in tl
        ), f"{rel} must mention network metadata path"
        assert (
            "fetch_pdf" in tl or "fetch" in tl or "stage_network" in tl
        ), f"{rel} must mention PDF fetch for network path"


def test_active_docs_do_not_recommend_copy_mode_for_normal_manual_stage():
    """Normal manual PDF staging examples should not use apply without move."""
    stale_copy_sop = "stage_raw_pdfs_to_paper_raw.py " + "--apply"
    for rel in [
        "README.md",
        "AGENTS.md",
        "CLAUDE.md",
        "docs/ARCHITECTURE.md",
        "docs/PROJECT_STATUS.md",
        "docs/PROJECT_CONTRACT.md",
        "docs/ZOTERO_INTEGRATION.md",
        "docs/PDF_RESOLVER_DESIGN.md",
        "skills/literature_library_manager/SKILL.md",
        "skills/literature_library_manager/CLAUDE.md",
    ]:
        text = _read(rel)
        assert stale_copy_sop not in text, (
            f"{rel} still recommends manual staging without --move"
        )


def test_fetch_resolver_action_hints_require_move_apply():
    for path in (_ROOT / "src" / "fetch" / "resolvers").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "stage_raw_pdfs_to_paper_raw.py --apply" not in text
        if "stage_raw_pdfs_to_paper_raw.py" in text:
            assert "--move --apply" in text, f"{path} has stale staging hint"
