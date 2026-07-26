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
* no package other than ``root`` lists ``root`` as an allowed target, and no
  sanctioned seam points at root either;
* ``ROOT_LEAVES`` pins the root membership, so a new ``src/*.py`` cannot
  quietly inherit root's import-anything privilege;
* every src subpackage carries ``__init__.py`` (namespace-package portions
  are invisible to the packaging config in ``pyproject.toml``);
* imports are only ever spelled as real import statements — a string import
  (``__import__``/``importlib.import_module``) would bypass this whole guard.

Extend sanctioned seams consciously, never casually: removing the reason
removes the sanction.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

pytestmark = pytest.mark.hygiene

# The only modules allowed to sit directly under src/ (the composition top).
ROOT_LEAVES = {"__init__.py", "server.py", "prompt_builder.py"}

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
    if len(parts) == 1:
        # bare ``import src`` — a reference to the composition root itself
        return "root"
    if len(parts) == 2:
        # ``src.X``: a package reference (``from src.discovery import x``)
        # when X is a known package, else a root-leaf module (``src.server``).
        return parts[1] if parts[1] in ALLOWED else "root"
    return parts[1]


def _containing_package(module: str) -> list[str]:
    """Parts of the package a module lives in (``src.a.b.c`` -> ``[src, a, b]``).

    ``src/a/b/__init__.py`` is named ``src.a.b.__init__`` here, so dropping the
    last component yields ``src.a.b`` for both packages and plain modules —
    exactly the base Python uses to resolve a level-1 relative import.
    """
    return module.split(".")[:-1]


def _resolve_relative(package_parts: list[str], level: int, module: str | None) -> str | None:
    """Absolute name of a relative import, or None when it escapes the tree."""
    keep = len(package_parts) - (level - 1)
    if keep < 1:
        return None
    base = ".".join(package_parts[:keep])
    return f"{base}.{module}" if module else base


def _collect_imports(
    tree: ast.AST, module: str
) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Return (eager, lazy) src.* imports, including relative and ``from src``.

    Eager = module/class level (executed at import time); lazy = inside a
    function or method body.
    """
    eager: list[tuple[int, str]] = []
    lazy: list[tuple[int, str]] = []
    package_parts = _containing_package(module)

    def record(in_func: bool, lineno: int, imported: str) -> None:
        if imported.startswith("src.") or imported == "src":
            (lazy if in_func else eager).append((lineno, imported))

    def visit(node: ast.AST, in_func: bool) -> None:
        for child in ast.iter_child_nodes(node):
            child_in_func = in_func or isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
            )
            if isinstance(child, ast.ImportFrom):
                if child.level:
                    resolved = _resolve_relative(package_parts, child.level, child.module)
                    if resolved:
                        record(in_func, child.lineno, resolved)
                elif child.module == "src":
                    # ``from src import naming`` names a package member, not
                    # the ``src`` root itself.
                    for alias in child.names:
                        record(in_func, child.lineno, f"src.{alias.name}")
                elif child.module:
                    record(in_func, child.lineno, child.module)
            elif isinstance(child, ast.Import):
                for alias in child.names:
                    record(in_func, child.lineno, alias.name)
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
    for importer, imported in SANCTIONED | SANCTIONED_LATE:
        if _package_of(imported) == "root":
            bad.append(f"{importer} -> {imported} is sanctioned into root")
    overlap = SANCTIONED & SANCTIONED_LATE
    assert not overlap, (
        "seams listed in both SANCTIONED and SANCTIONED_LATE — the lazy-only "
        f"constraint is then unenforced: {sorted(overlap)}"
    )
    assert not bad, "ALLOWED table invariant broken:\n  " + "\n  ".join(bad)


def test_root_membership_is_pinned():
    """A new src/*.py must not silently inherit root's import-anything rights."""
    leaves = {p.name for p in SRC.glob("*.py")}
    assert leaves == ROOT_LEAVES, (
        "src/ root membership changed — root may import anything, so a new leaf "
        "needs a conscious decision (move it into a package, or extend "
        f"ROOT_LEAVES with a reason). Found: {sorted(leaves)}"
    )


def test_every_src_subpackage_has_init():
    """Namespace-package portions are invisible to pyproject's package finder."""
    missing = [
        d.relative_to(ROOT).as_posix()
        for d in sorted(SRC.rglob("*"))
        if d.is_dir()
        and d.name != "__pycache__"
        and any(d.glob("*.py"))
        and not (d / "__init__.py").exists()
    ]
    assert not missing, (
        "src subpackages without __init__.py (setuptools find= would drop them "
        "from the wheel):\n  " + "\n  ".join(missing)
    )


def test_no_string_imports_in_src():
    """String imports would bypass the AST-based layering guard entirely."""
    pattern = re.compile(r"\b(?:__import__|importlib\.import_module)\s*\(")
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{lineno}: {line.strip()}")
    assert not offenders, (
        "string imports in src/ (use a real import statement so layering stays "
        "checkable):\n  " + "\n  ".join(offenders)
    )


def test_collector_sees_relative_and_from_src_imports():
    """Import spellings the guard used to be blind to must reach the classifier."""
    source = (
        "from ..discovery import models\n"
        "from src import server\n"
        "from . import base\n"
        "def later():\n"
        "    from ...writer import bib\n"
    )
    eager, lazy = _collect_imports(ast.parse(source), "src.fetch.resolvers.oa_resolvers")
    assert [imported for _, imported in eager] == [
        "src.fetch.discovery",  # ".." from src.fetch.resolvers -> src.fetch
        "src.server",
        "src.fetch.resolvers",
    ]
    assert [imported for _, imported in lazy] == ["src.writer"]
    assert _package_of("src.server") == "root"


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
        eager, lazy = _collect_imports(
            ast.parse(path.read_text(encoding="utf-8")), module
        )
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
