"""Hygiene guard — the src/ package layering DAG.

Encodes the target architecture as data: for every src package, the set of
src packages it may import at MODULE TOP LEVEL.  A new upward edge fails
this test with the exact offending import line.

Layer order (low → high):
    utils < root-leaf < metadata < library ~ workspace < catalog < ingest
    < catalog_folders < fetch < discovery < metadata_resolve < services
    < server/writer

Sanctioned exceptions are listed per package below with their reasons —
extend them consciously, never casually.  Late (in-function) imports are
deliberately NOT scanned here: the remaining ones are documented seams, and
scanning them would freeze implementation details rather than architecture.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

pytestmark = pytest.mark.hygiene

# Package → packages it may import from src.* at module top level.
# "root" covers single-module leaves directly under src/ (naming, path_utils,
# file_fingerprint, bib, prompt_builder, logging_setup, converter, cleaner,
# mineru_lock, mineru_runtime, mineru_service_manager, mineru_smoke, server).
ALLOWED: dict[str, set[str]] = {
    "utils": set(),
    "metadata": {"utils", "root"},
    "library": {"utils", "metadata", "catalog", "workspace", "root"},
    "workspace": {"utils", "metadata", "library", "root"},
    "catalog": {"utils", "metadata", "root"},
    "ingest": {"utils", "metadata", "library", "workspace", "catalog",
               "catalog_folders", "root"},
    "catalog_folders": {"utils", "library", "catalog", "discovery", "root"},
    "fetch": {"utils", "metadata", "root"},
    "discovery": {"utils", "metadata", "library", "workspace", "ingest",
                  "fetch", "root"},
    "metadata_resolve": {"utils", "metadata", "library", "workspace",
                         "ingest", "fetch", "discovery", "root"},
    "services": {"utils", "metadata", "library", "workspace", "catalog",
                 "ingest", "catalog_folders", "fetch", "discovery",
                 "metadata_resolve", "root"},
    "writer": {"utils", "metadata", "library", "catalog", "catalog_folders",
               "services", "root"},
    "root": {"utils", "metadata", "library", "workspace", "catalog", "ingest",
             "catalog_folders", "fetch", "discovery", "metadata_resolve",
             "services", "writer", "root"},
}


# Sanctioned single-module seams (importer module -> imported module).
# Each carries a reason; removing the reason removes the sanction.
SANCTIONED: set[tuple[str, str]] = {
    # PaperCandidate is the shared candidate value object; the plan keeps it
    # in discovery.models because of its discovery-only fields, and the
    # metadata match layer consumes candidate values to build receipts.
    ("src.metadata.pdf_identity", "src.discovery.models"),
    ("src.metadata.pdf_match", "src.discovery.models"),
    # CLAUDE.md mandates ALL OpenAlex/Crossref HTTP goes through the unified
    # ProviderClient, which lives in discovery.providers — fetch obeys it.
    ("src.fetch.fetch_openalex", "src.discovery.providers.provider_client"),
    ("src.fetch.fetch_publisher", "src.discovery.resolve_crossref"),
    # The staging gateway is discovery's composition boundary into the app
    # staging service (single call site; the service stages INTO paper_raw).
    ("src.discovery.staging_gateway", "src.services.network_metadata_staging"),
}


def _package_of(module: str) -> str | None:
    parts = module.split(".")
    if not parts or parts[0] != "src":
        return None
    if len(parts) == 2:
        return "root"
    return parts[1]


def _top_level_src_imports(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in tree.body:  # top level only — function bodies excluded
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("src."):
            found.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("src."):
                    found.append((node.lineno, alias.name))
    return found


def test_src_package_layering_dag():
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        module = rel[:-3].replace("/", ".")
        pkg = _package_of(module)
        if pkg is None:
            continue
        allowed = ALLOWED.get(pkg)
        assert allowed is not None, f"unknown src package {pkg!r} — extend ALLOWED"
        for lineno, imported in _top_level_src_imports(path):
            target = _package_of(imported)
            if target is None or target == pkg:
                continue
            if target not in allowed and (module, imported) not in SANCTIONED:
                violations.append(f"{rel}:{lineno}: {pkg} -> {target} ({imported})")
    assert not violations, (
        "layering violations (extend ALLOWED consciously if intended):\n  "
        + "\n  ".join(violations)
    )
