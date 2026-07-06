from __future__ import annotations

import ast
from pathlib import Path

import pytest


pytestmark = [pytest.mark.hygiene, pytest.mark.security]

ROOT = Path(__file__).resolve().parents[2]


def _test_files() -> list[Path]:
    return [
        path
        for path in (ROOT / "tests").glob("**/*.py")
        if "__pycache__" not in path.parts
        and path.name != "test_no_runtime_root_writes.py"
    ]


def _string_literals(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            values.append(node.value.replace("\\", "/"))
    return values


def test_tests_do_not_reference_root_mineru_cache_as_real_cache():
    offenders: list[str] = []
    for path in _test_files():
        for value in _string_literals(path):
            if value == "output/mineru_cache" or value.endswith("/output/mineru_cache"):
                offenders.append(str(path.relative_to(ROOT)))
    assert not offenders


def test_tests_do_not_instantiate_ingest_services_with_default_runtime_roots():
    forbidden_snippets = [
        "PaperRawAllocator(PAPER_RAW_DIR",
        "PaperRawFormalizationService()",
        "V2PaperCommitService()",
        "PaperRawConverter()",
    ]
    offenders: list[str] = []
    for path in _test_files():
        text = path.read_text(encoding="utf-8")
        for snippet in forbidden_snippets:
            if snippet in text:
                offenders.append(f"{path.relative_to(ROOT)}: {snippet}")
    assert not offenders
