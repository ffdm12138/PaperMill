"""Hygiene guard — the src/ package layering DAG.

Encodes the target architecture as data: for every src package, the set of
src packages it may import.  Module-level (eager) imports are checked against
``ALLOWED`` plus the per-module ``SANCTIONED`` seams; function-body (lazy)
imports are additionally allowed through ``SANCTIONED_LATE``.  A new upward
edge fails this test with the exact offending import line.

Layer order (low → high)::

    utils < mineru < metadata < catalog ~ workspace < library < ingest
          < catalog_folders < fetch < discovery < metadata_resolve
          < staging < writer < root

``root`` is the composition top: only ``src/server.py`` and
``src/prompt_builder.py`` live there.  Root may import anything; NOTHING may
import root.  Structural invariants below make the table self-checking:

* every ``ALLOWED`` edge points strictly downward in ``LAYER_ORDER`` —
  cycles are representable only via ``SANCTIONED``/``SANCTIONED_LATE``,
  each with a written reason;
* no package other than ``root`` lists ``root`` as an allowed target.

Extend sanctioned seams consciously, never casually: removing the reason
removes the sanction.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

pytestmark = pytest.mark.hygiene

# Low → high.  catalog ~ workspace are unordered peers (neither imports the
# other); their relative position here is fixed for determinism only.
LAYER_ORDER = [
    "utils", "mineru", "metadata", "catalog", "workspace", "library",
    "ingest", "catalog_folders", "fetch", "discovery", "metadata_resolve",
    "staging", "writer", "root",
]

# Package → packages it may import from src.* (eager or lazy).
ALLOWED: dict[str, set[str]] = {
    "utils": set(),
    "mineru": {"utils"},
    "metadata": {"utils"},
    "catalog": {"utils", "metadata"},
    "workspace": {"utils", "metadata"},
    "library": {"utils", "metadata", "catalog"},
    "ingest": {"utils", "mineru", "metadata", "catalog", "workspace",
               "library"},
    "catalog_folders": {"utils", "catalog", "library"},
    "fetch": {"utils", "metadata"},
    "discovery": {"utils", "metadata", "workspace", "library", "ingest",
                  "fetch"},
    "metadata_resolve": {"utils", "metadata", "workspace", "library",
                         "ingest", "fetch", "discovery"},
    "staging": {"utils", "metadata", "library", "discovery"},
    "writer": {"utils", "metadata", "catalog", "library", "catalog_folders"},
    # root = src/server.py + src/prompt_builder.py (+ src/__init__.py):
    # the composition top.  May import anything; NOTHING may import root.
    "root": {"utils", "mineru", "metadata", "catalog", "workspace", "library",
             "ingest", "catalog_folders", "fetch", "discovery",
             "metadata_resolve", "staging", "writer", "root"},
}


# Sanctioned single-module seams (importer module -> imported module),
# eager or lazy.  Each carries a reason; removing the reason removes the
# sanction.
SANCTIONED: set[tuple[str, str]] = {
    # PaperCandidate/normalize_doi live in discovery.models because of their
    # discovery-only fields; the metadata match layer consumes candidate
    # values to build receipts.
    ("src.metadata.pdf_identity", "src.discovery.models"),
    ("src.metadata.pdf_match", "src.discovery.models"),
    # CLAUDE.md mandates ALL OpenAlex/Crossref HTTP goes through the unified
    # ProviderClient, which lives in discovery.providers — fetch obeys it.
    ("src.fetch.fetch_openalex", "src.discovery.providers.provider_client"),
    ("src.fetch.fetch_publisher", "src.discovery.resolve_crossref"),
    # The staging gateway is discovery's composition boundary into the app
    # staging service (single call site; the service stages INTO paper_raw).
    ("src.discovery.staging_gateway", "src.staging.network_metadata_staging"),
    # The keyword/notebook contract (keyword_id, normalize_keyword,
    # validate_notebook) is owned by discovery.contracts; catalog_folders
    # consumes contract functions for category identity.  Moving the
    # contract down would drag relevance/backfill machinery with it — the
    # 3-edge seam is deliberate and fail-closed elsewhere.
    ("src.catalog_folders.registry", "src.discovery.contracts.notebook"),
    ("src.catalog_folders.registry_schema", "src.discovery.contracts.notebook"),
    ("src.catalog_folders.validation", "src.discovery.contracts.notebook"),
    # library ~ workspace declared 2-cycle, eager leg: the ledger reads the
    # unified workspace lifecycle inspection to classify folders.
    ("src.library.paper_number_ledger", "src.workspace.lifecycle"),
}

# Lazy-only sanctioned seams: legal ONLY as function-body imports.  These are
# deliberate runtime seams, not architecture; keep them lazy.
SANCTIONED_LATE: set[tuple[str, str]] = {
    # library ~ workspace declared 2-cycle, lazy leg: readiness reads ledger
    # state constants at evaluation time.
    ("src.workspace.readiness", "src.library.paper_number_state"),
    # catalog_folders -> discovery lazy seams (runtime resolution).
    ("src.catalog_folders.reader", "src.discovery.runtime_context"),
    ("src.catalog_folders.registry", "src.discovery.contracts.notebook"),
    # ingest -> catalog_folders: commit/rollback request category-folder
    # reconciliation AFTER the transaction commit point; lazy keeps the
    # transaction core importable without the folder machinery.
    ("src.ingest.commit", "src.catalog_folders.formal_registry"),
    ("src.ingest.commit", "src.catalog_folders.reconcile"),
    ("src.ingest.commit", "src.catalog_folders.task_planner"),
    ("src.ingest.rollback", "src.catalog_folders.formal_registry"),
    ("src.ingest.rollback", "src.catalog_folders.reconcile"),
    # ingest -> fetch: persisted stage manifests strip URL query strings via
    # the fetch-layer sanitizer (single implementation, report-safety rule).
    ("src.ingest.stage_manifest", "src.fetch.pdf_transport"),
}


def _package_of(module: str) -> str | None:
    parts = module.split(".")
    if not parts or parts[0] != "src":
        return None
    if len(parts) == 2:
        # ``src.X``: a package reference (``from src.discovery import x``)
        # when X is a known package, else a root-leaf module (``src.server``).
        return parts[1] if parts[1] in ALLOWED else "root"
    return parts[1]


def _collect_imports(tree: ast.AST) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Return (eager, lazy) src.* imports.

    Eager = module/class level (executed at import time); lazy = inside a
    function or method body.
    """
    eager: list[tuple[int, str]] = []
    lazy: list[tuple[int, str]] = []

    def visit(node: ast.AST, in_func: bool) -> None:
        for child in ast.iter_child_nodes(node):
            child_in_func = in_func or isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
            )
            if isinstance(child, ast.ImportFrom):
                if child.module and child.module.startswith("src."):
                    (lazy if in_func else eager).append((child.lineno, child.module))
            elif isinstance(child, ast.Import):
                for alias in child.names:
                    if alias.name.startswith("src."):
                        (lazy if in_func else eager).append((child.lineno, alias.name))
            visit(child, child_in_func)

    visit(tree, False)
    return eager, lazy


def test_allowed_table_is_a_strict_downward_dag():
    """ALLOWED edges must point strictly downward; cycles only via SANCTIONED."""
    index = {name: i for i, name in enumerate(LAYER_ORDER)}
    assert set(ALLOWED) == set(LAYER_ORDER), (
        "ALLOWED keys must exactly match LAYER_ORDER"
    )
    bad: list[str] = []
    for pkg, targets in ALLOWED.items():
        if pkg == "root":
            continue
        if "root" in targets:
            bad.append(f"{pkg} lists 'root' — nothing may import root")
        for target in targets:
            if index[target] >= index[pkg]:
                bad.append(f"{pkg} -> {target} is not strictly downward")
    assert not bad, "ALLOWED table invariant broken:\n  " + "\n  ".join(bad)


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
        eager, lazy = _collect_imports(ast.parse(path.read_text(encoding="utf-8")))
        for lineno, imported in eager:
            target = _package_of(imported)
            if target is None or target == pkg:
                continue
            if target not in allowed and (module, imported) not in SANCTIONED:
                violations.append(f"{rel}:{lineno}: {pkg} -> {target} ({imported})")
        for lineno, imported in lazy:
            target = _package_of(imported)
            if target is None or target == pkg:
                continue
            if (
                target not in allowed
                and (module, imported) not in SANCTIONED
                and (module, imported) not in SANCTIONED_LATE
            ):
                violations.append(
                    f"{rel}:{lineno}: {pkg} -> {target} ({imported}) [lazy]"
                )
    assert not violations, (
        "layering violations (extend ALLOWED/SANCTIONED consciously if intended):\n  "
        + "\n  ".join(violations)
    )
