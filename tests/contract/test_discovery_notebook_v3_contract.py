from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_FIELDS = {
    "keyword",
    "expansions",
    "expanded_queries",
    "provider_state",
    "active_expansions",
    "expanded_query",
    "original_keyword",
    "expansion_id",
}

# The migration parser is the sole intentional legacy boundary.  The strict
# v3 validator may name rejected fields, but active consumers and command
# paths must never read them.
MIGRATION_PATHS = {ROOT / "src" / "discovery" / "notebook_v3_migration.py"}
ACTIVE_PATHS = [
    path
    for directory in (ROOT / "src" / "discovery", ROOT / "src" / "catalog_folders")
    for path in sorted(directory.rglob("*.py"))
    if path not in MIGRATION_PATHS
] + [
    ROOT / "scripts" / name
    for name in (
        "audit_discovery_keyword_index_sources.py",
        "recover_discovery_keyword_notebooks.py",
        "manage_discovery_keywords.py",
        "migrate_keyword_notebooks_v3.py",
    )
]


def _legacy_reads(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            key = node.slice.value if isinstance(node.slice, ast.Constant) else None
            if key in FORBIDDEN_FIELDS:
                findings.append((node.lineno, str(key)))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"get", "pop", "setdefault"} and node.args:
                key = node.args[0].value if isinstance(node.args[0], ast.Constant) else None
                receiver = node.func.value
                receiver_name = receiver.id if isinstance(receiver, ast.Name) else None
                if key in FORBIDDEN_FIELDS and receiver_name in {"data", "payload", "notebook", "page", "value"}:
                    findings.append((node.lineno, str(key)))
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_FIELDS:
            findings.append((node.lineno, node.attr))
    return findings


@pytest.mark.contract
def test_active_cli_is_v3_only():
    findings = {
        str(path.relative_to(ROOT)): _legacy_reads(path)
        for path in ACTIVE_PATHS
        if _legacy_reads(path)
    }
    assert findings == {}




