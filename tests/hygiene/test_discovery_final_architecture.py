"""Hygiene test: discovery final architecture verifier.

Runs the AST-based verifier and asserts zero forbidden patterns.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import scripts.verify_discovery_final_architecture as verifier


# ── Post-migration hardening gates: positive + negative coverage ─────────


_HEALTHY_WORKSPACE = '''
from filelock import FileLock
MIGRATION_LOCK_PATH = DISCOVERY_MIGRATIONS_DIR / ".migration.lock"
previous_pointer_snapshot_path = None
raise CutoverReconciliationError("x")
if verify_tree:
    computed_tree = hash_workspace_tree(gen_root, exclude={"workspace.json"})
    if computed_tree != manifest.workspace_tree_sha256:
        raise WorkspaceManifestMismatchError("x")
'''


def _build_healthy_tree(root: Path) -> dict[str, Path]:
    """Minimal src/scripts tree satisfying every kept hardening gate."""
    src_root = root / "src"
    src = src_root / "discovery"
    scripts = root / "scripts"
    files = {
        (src / "workspace.py"): _HEALTHY_WORKSPACE,
        (src / "contracts" / "manifest.py"): "previous_generation_id = None\n",
    }
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    scripts.mkdir(parents=True, exist_ok=True)
    return {"SRC_ROOT": src_root, "SRC": src, "SCRIPTS": scripts}


@pytest.fixture
def healthy_tree(tmp_path, monkeypatch):
    dirs = _build_healthy_tree(tmp_path)
    monkeypatch.setattr(verifier, "SRC_ROOT", dirs["SRC_ROOT"])
    monkeypatch.setattr(verifier, "SRC", dirs["SRC"])
    monkeypatch.setattr(verifier, "SCRIPTS", dirs["SCRIPTS"])
    return dirs


def _gate_errors(dirs) -> list:
    report = verifier.VerifierReport()
    verifier._check_migration_hardening_rules(report)
    return report.errors


class TestMigrationHardeningGates:
    """Each kept hardening gate must catch its violation (and pass clean)."""

    def test_healthy_tree_passes(self, healthy_tree):
        assert _gate_errors(healthy_tree) == []

    def test_gate1_old_module_path_import_caught(self, healthy_tree):
        evil = healthy_tree["SRC"] / "evil.py"
        evil.write_text(
            "from src.discovery.page_journal import PageJournal\n", encoding="utf-8"
        )
        errors = _gate_errors(healthy_tree)
        assert any("src.discovery.page_journal" in e.message for e in errors)

    def test_gate3_missing_cutover_lock_caught(self, healthy_tree):
        ws = healthy_tree["SRC"] / "workspace.py"
        ws.write_text(_HEALTHY_WORKSPACE.replace('".migration.lock"', '"other"'),
                      encoding="utf-8")
        errors = _gate_errors(healthy_tree)
        assert any(".migration.lock" in e.message for e in errors)

    def test_gate3_missing_previous_generation_id_caught(self, healthy_tree):
        manifest = healthy_tree["SRC"] / "contracts" / "manifest.py"
        manifest.write_text("generation_id = ''\n", encoding="utf-8")
        errors = _gate_errors(healthy_tree)
        assert any("previous_generation_id" in e.message for e in errors)

    def test_gate4_missing_tree_verification_caught(self, healthy_tree):
        ws = healthy_tree["SRC"] / "workspace.py"
        ws.write_text(
            _HEALTHY_WORKSPACE.replace("manifest.workspace_tree_sha256", "expected"),
            encoding="utf-8",
        )
        errors = _gate_errors(healthy_tree)
        assert any("workspace_tree_sha256" in e.message for e in errors)

    def test_gate6_legacy_candidate_seeds_caught(self, healthy_tree):
        evil = healthy_tree["SCRIPTS"] / "evil.py"
        evil.write_text("x = 'legacy_candidate_seeds'\n", encoding="utf-8")
        errors = _gate_errors(healthy_tree)
        assert any("legacy_candidate_seeds" in e.message for e in errors)

    def test_gate6_pending_store_reintroduction_caught(self, healthy_tree):
        evil = healthy_tree["SRC"] / "evil_store.py"
        evil.write_text(
            "from y import PendingCandidateStoreV4\n", encoding="utf-8"
        )
        errors = _gate_errors(healthy_tree)
        assert any("PendingCandidateStoreV4" in e.message for e in errors)


# ── Post-migration final-state gates (12, 15, 16): coverage ──────────────


_HEALTHY_FINAL_COORDINATOR = '''
def run_batch():
    try:
        work()
    except Exception as exc:
        logger.error("lane failed: %s", exc)
        raise
'''

# Current-version constants of their own artifact families — Gate 16 must
# not flag these (only legacy "3.0" acceptance is forbidden).
_HEALTHY_VERSION_CONSTANTS = '''
PAGINATION_SCHEMA_VERSION = "2.0"
RECEIPT_SCHEMA_VERSION = "1.0"
RELEVANCE_PROFILE_SCHEMA_VERSION = "1.0"
'''

_HEALTHY_REJECTION_BRANCH = '''
def validate_notebook(data):
    version = str(data.get("schema_version") or "")
    if version in ("1.0", "2.0", "3.0"):
        raise UnsupportedNotebookSchemaError(
            f"notebook schema {version} must be migrated to v4"
        )
    if version != "4.0":
        raise UnsupportedNotebookSchemaError("unsupported")
    return data
'''


def _build_healthy_final_tree(root: Path) -> dict[str, Path]:
    """Minimal tree satisfying every kept final-state gate."""
    src_root = root / "src"
    src = src_root / "discovery"
    scripts = root / "scripts"
    files = {
        (scripts / "discover_papers.py"): (
            "assert_discovery_write_allowed()\n"
        ),
        (scripts / "discover_papers_concurrent.py"): (
            "assert_discovery_write_allowed()\n"
        ),
        (src / "coordinator.py"): _HEALTHY_FINAL_COORDINATOR,
        (src / "maintenance_gate.py"): (
            "def assert_discovery_write_allowed(lock_path=None):\n"
            "    return None\n"
        ),
        (src / "runtime" / "batch_runtime.py"): "class DiscoveryBatchRuntime:\n    pass\n",
        (src / "contracts" / "notebook.py"): _HEALTHY_REJECTION_BRANCH,
        (src / "contracts" / "page_journal.py"): _HEALTHY_VERSION_CONSTANTS,
    }
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return {"SRC_ROOT": src_root, "SRC": src, "SCRIPTS": scripts}


@pytest.fixture
def healthy_final_tree(tmp_path, monkeypatch):
    dirs = _build_healthy_final_tree(tmp_path)
    monkeypatch.setattr(verifier, "SRC_ROOT", dirs["SRC_ROOT"])
    monkeypatch.setattr(verifier, "SRC", dirs["SRC"])
    monkeypatch.setattr(verifier, "SCRIPTS", dirs["SCRIPTS"])
    return dirs


def _final_gate_errors(dirs) -> list:
    report = verifier.VerifierReport()
    verifier._check_v4_migration_final_rules(report)
    return report.errors


class TestMigrationFinalGates:
    """Each kept final-state gate (12, 15, 16) must catch its violation."""

    def test_healthy_tree_passes(self, healthy_final_tree):
        assert _final_gate_errors(healthy_final_tree) == []

    # ── Gate 12 ──

    def test_gate12_legacy_symbol_caught(self, healthy_final_tree):
        evil = healthy_final_tree["SRC"] / "evil.py"
        evil.write_text("x = is_legacy_unbound_profile(p)\n", encoding="utf-8")
        errors = _final_gate_errors(healthy_final_tree)
        assert any("is_legacy_unbound_profile" in e.message for e in errors)

    def test_gate12_bare_except_exception_caught(self, healthy_final_tree):
        coord = healthy_final_tree["SRC"] / "coordinator.py"
        coord.write_text(
            "def run():\n"
            "    try:\n"
            "        work()\n"
            "    except Exception:\n"
            "        pass\n",
            encoding="utf-8",
        )
        errors = _final_gate_errors(healthy_final_tree)
        assert any("except Exception" in e.message for e in errors)

    def test_gate12_coordinator_schema3_whitelist_caught(self, healthy_final_tree):
        coord = healthy_final_tree["SRC"] / "coordinator.py"
        coord.write_text(
            _HEALTHY_FINAL_COORDINATOR
            + '\nSCHEMAS = ("3.0", "4.0")\n',
            encoding="utf-8",
        )
        errors = _final_gate_errors(healthy_final_tree)
        assert any("schema '3.0'" in e.message for e in errors)

    def test_gate12_batch_runtime_compat_alias_caught(self, healthy_final_tree):
        br = healthy_final_tree["SRC"] / "runtime" / "batch_runtime.py"
        br.write_text(
            "# backward compat re-export\n"
            "from x import PageJournalStoreV4 as PageJournalStore\n",
            encoding="utf-8",
        )
        errors = _final_gate_errors(healthy_final_tree)
        assert any("PageJournalStore" in e.message for e in errors)

    # ── Gate 15 ──

    def test_gate15_writer_without_block_check_caught(self, healthy_final_tree):
        writer = healthy_final_tree["SCRIPTS"] / "discover_papers_concurrent.py"
        writer.write_text("def main():\n    return 0\n", encoding="utf-8")
        errors = _final_gate_errors(healthy_final_tree)
        assert any("discover_papers_concurrent.py" in e.file
                   and "assert_discovery_write_allowed" in e.message
                   for e in errors)

    def test_gate15_workspace_root_bypass_caught(self, healthy_final_tree):
        writer = healthy_final_tree["SCRIPTS"] / "discover_papers.py"
        writer.write_text(
            "if not args.workspace_root:\n"
            "    assert_discovery_write_allowed()\n",
            encoding="utf-8",
        )
        errors = _final_gate_errors(healthy_final_tree)
        assert any("discover_papers.py" in e.file
                   and "--workspace-root" in e.message
                   for e in errors)

    def test_gate15_production_importing_migrations_caught(self, healthy_final_tree):
        evil = healthy_final_tree["SRC"] / "evil_import.py"
        evil.write_text(
            "from src.migrations.discovery_v4.maintenance_lock import x\n",
            encoding="utf-8",
        )
        errors = _final_gate_errors(healthy_final_tree)
        assert any("evil_import.py" in e.file
                   and "migration package" in e.message for e in errors)

    def test_gate15_production_tool_flat_constant_caught(self, healthy_final_tree):
        tool = healthy_final_tree["SCRIPTS"] / "manage_discovery_keywords.py"
        tool.write_text(
            "from config.settings import DISCOVERY_KEYWORD_NOTEBOOK_DIR\n",
            encoding="utf-8",
        )
        errors = _final_gate_errors(healthy_final_tree)
        assert any("manage_discovery_keywords.py" in e.file
                   and "DISCOVERY_KEYWORD_NOTEBOOK_DIR" in e.message
                   for e in errors)

    # ── Gate 16 ──

    def test_gate16_accept_branch_caught(self, healthy_final_tree):
        evil = healthy_final_tree["SRC"] / "evil_load.py"
        evil.write_text(
            "def load(data):\n"
            '    if data.get("schema_version") == "3.0":\n'
            "        return parse_v3(data)\n",
            encoding="utf-8",
        )
        errors = _final_gate_errors(healthy_final_tree)
        assert any("schema '3.0'" in e.message for e in errors)

    def test_gate16_wider_tuple_accept_caught(self, healthy_final_tree):
        evil = healthy_final_tree["SRC"] / "evil_wide.py"
        evil.write_text(
            'if version not in ("1.0", "2.0", "3.0", "4.0"):\n'
            "    raise ValueError('bad')\n",
            encoding="utf-8",
        )
        errors = _final_gate_errors(healthy_final_tree)
        assert any("schema '3.0'" in e.message for e in errors)

    def test_gate16_rejection_tuple_without_raise_caught(self, healthy_final_tree):
        nb = healthy_final_tree["SRC"] / "contracts" / "notebook.py"
        nb.write_text(
            'if version in ("1.0", "2.0", "3.0"):\n'
            "    data = migrate_inline(data)\n",
            encoding="utf-8",
        )
        errors = _final_gate_errors(healthy_final_tree)
        assert any("rejected, never parsed" in e.message for e in errors)


def test_discovery_final_architecture_passes() -> None:
    """The AST-based architecture verifier must report zero errors."""
    from scripts.verify_discovery_final_architecture import (
        verify_discovery_final_architecture,
    )

    report = verify_discovery_final_architecture()

    errors = report.errors
    if errors:
        msg_lines = [f"{len(errors)} architecture violation(s):"]
        for f in errors:
            msg_lines.append(f"  {f.file}:{f.line}: [{f.category}] {f.message}")
        pytest.fail("\n".join(msg_lines))

    # Report must have scanned at least the core discovery files
    scanned = set(report.files_scanned)
    required = {
        "src/discovery/coordinator.py",
        "src/discovery/execution/lane_executor.py",
        "src/discovery/providers/provider_client.py",
        "src/discovery/backfill_transaction.py",
        "src/discovery/contracts/notebook.py",
        "src/discovery/reporting/report_builder.py",
    }
    missing = required - scanned
    assert not missing, f"Verifier did not scan required files: {missing}"


def test_official_entrypoint_dispatches_typed_lanes_and_builds_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dynamic companion to the AST verifier.

    The public coordinator must schedule every physical lane through the typed
    executors and hand all outcomes to one final ``ReportBuilder.build`` call;
    no compatibility callback or per-lane report builder is allowed.  This test
    exercises the v4 single-stack composition root with an explicit
    ``DiscoveryWorkspace`` + ``DiscoveryStoreBundleV4``.
    """
    import src.discovery.coordinator as coordinator
    from src.discovery.coordinator import (
        DiscoveryOptions,
        DiscoveryRuntimeDependencies,
        run_discovery_batch_with_dependencies,
    )
    from src.discovery.providers.provider_page_fetcher import CallbackProviderPageFetcher
    from src.discovery.reporting.report_builder import ReportBuilder as RealReportBuilder
    from src.discovery.staging_gateway import MetadataStagingGateway
    from src.discovery.stores.bundle import DiscoveryStoreBundleV4
    from src.discovery.stores.notebook_store import NotebookStoreV4
    from src.discovery.workspace import DiscoveryWorkspace
    from tests.helpers.fake_provider import discovery_page
    from tests.helpers.relevance_profiles import bind_test_relevance_profile

    workspace = DiscoveryWorkspace(
        generation_id="hygiene-test",
        root=tmp_path / "workspace",
        keyword_notebook_dir=tmp_path / "workspace" / "keyword_notebooks",
        lane_states_dir=tmp_path / "workspace" / "lane_states",
        page_journals_dir=tmp_path / "workspace" / "page_journals",
        indexes_dir=tmp_path / "workspace" / "indexes",
        exports_dir=tmp_path / "workspace" / "exports",
        reports_dir=tmp_path / "workspace" / "reports",
        locks_dir=tmp_path / "workspace" / "locks",
    )
    workspace.ensure_dirs()

    bundle = DiscoveryStoreBundleV4.from_workspace(workspace)
    store = NotebookStoreV4(workspace)
    store.ensure_notebook("风吹雪")
    store.sync_search_queries("风吹雪", add=[
        {"query": "风吹雪", "language": "zh"},
        {"query": "blowing snow", "language": "en"},
    ])
    bind_test_relevance_profile(store, "风吹雪")
    store.set_enabled("风吹雪", True)

    dispatched: list[tuple[str, str, str]] = []

    def fetch(spec, cursor, _client):
        dispatched.append((spec.key.mode, spec.key.provider, spec.key.query_id))
        return discovery_page(
            provider=spec.key.provider,
            keyword_zh=spec.keyword_zh,
            query=spec.query,
            lane=spec.key.mode,
            cursor=cursor,
            query_id=spec.key.query_id,
            query_language=spec.query_language,
            candidates=[],
            exhausted=True,
        )

    refresh_specs: list[object] = []
    backfill_specs: list[object] = []
    real_refresh = coordinator.execute_refresh_lane
    real_backfill = coordinator.execute_backfill_lane

    def spy_refresh(spec, **kwargs):
        refresh_specs.append(spec)
        return real_refresh(spec, **kwargs)

    def spy_backfill(spec, **kwargs):
        backfill_specs.append(spec)
        return real_backfill(spec, **kwargs)

    builds: list[object] = []

    class SpyReportBuilder:
        def __init__(self) -> None:
            self._delegate = RealReportBuilder()

        def build(self, **kwargs):
            builds.append(kwargs)
            return self._delegate.build(**kwargs)

    monkeypatch.setattr(coordinator, "execute_refresh_lane", spy_refresh)
    monkeypatch.setattr(coordinator, "execute_backfill_lane", spy_backfill)
    monkeypatch.setattr(coordinator, "ReportBuilder", SpyReportBuilder)

    deps = DiscoveryRuntimeDependencies(
        bundle=bundle,
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        locks_dir=workspace.locks_dir,
        exports_dir=workspace.exports_dir,
        output_dir=tmp_path / "output",
        relevance_cache_dir=tmp_path / "relevance_cache",
        title_resolution_cache_dir=tmp_path / "title_cache",
        metadata_gateway=MetadataStagingGateway(
            paper_raw_dir=tmp_path / "paper_raw",
            papers_dir=tmp_path / "papers",
            ledger_path=tmp_path / "ledger.json",
        ),
    )
    options = DiscoveryOptions(
        mode="hybrid",
        refresh_pages=1,
        backfill_pages=1,
        max_candidates=8,
    )
    report = run_discovery_batch_with_dependencies(
        ["风吹雪"],
        deps=deps,
        options=options,
        max_workers=1,
        page_fetcher=CallbackProviderPageFetcher(fetch),
    )

    assert report.exit_code == 0
    assert len(refresh_specs) == 4
    assert len(backfill_specs) == 4
    assert len({spec.key.stable_id() for spec in [*refresh_specs, *backfill_specs]}) == 8
    assert len(dispatched) == 8
    assert len(builds) == 1


# ── Unified-HTTP + retired flat-path gates: positive + negative coverage ─


def _build_http_gate_tree(root: Path) -> dict[str, Path]:
    """Minimal tree satisfying the unified-HTTP / flat-path gates."""
    src_root = root / "src"
    src = src_root / "discovery"
    scripts = root / "scripts"
    files = {
        (src / "providers" / "provider_client.py"): (
            "import requests  # the single allowed requests call-site\n"
        ),
        (src / "clean_module.py"): (
            "from urllib.parse import urlparse\n"
            "def useful():\n"
            "    return urlparse('https://example.org')\n"
        ),
        (scripts / "audit_discovery_keyword_index_sources.py"): (
            "from src.discovery.runtime_context import resolve_active_runtime\n"
        ),
    }
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return {"SRC_ROOT": src_root, "SRC": src, "SCRIPTS": scripts}


@pytest.fixture
def http_gate_tree(tmp_path, monkeypatch):
    dirs = _build_http_gate_tree(tmp_path)
    monkeypatch.setattr(verifier, "SRC_ROOT", dirs["SRC_ROOT"])
    monkeypatch.setattr(verifier, "SRC", dirs["SRC"])
    monkeypatch.setattr(verifier, "SCRIPTS", dirs["SCRIPTS"])
    return dirs


def _http_gate_errors(dirs) -> list:
    report = verifier.VerifierReport()
    verifier._check_http_and_flat_path_rules(report)
    return report.errors


class TestHttpAndFlatPathGates:
    """Gate H1/H2 must catch violations and pass on a clean tree."""

    def test_healthy_tree_passes(self, http_gate_tree):
        assert _http_gate_errors(http_gate_tree) == []

    def test_gate_h1_requests_import_caught(self, http_gate_tree):
        bad = http_gate_tree["SRC"] / "sneaky.py"
        bad.write_text("import requests\n", encoding="utf-8")
        errors = _http_gate_errors(http_gate_tree)
        assert any("direct HTTP import" in e.message and "sneaky" in e.file for e in errors)

    def test_gate_h1_httpx_from_import_caught(self, http_gate_tree):
        bad = http_gate_tree["SRC"] / "sneaky.py"
        bad.write_text("from httpx import get\n", encoding="utf-8")
        errors = _http_gate_errors(http_gate_tree)
        assert any("direct HTTP import" in e.message for e in errors)

    def test_gate_h1_urllib_request_caught(self, http_gate_tree):
        bad = http_gate_tree["SRC"] / "sneaky.py"
        bad.write_text("from urllib.request import urlopen\n", encoding="utf-8")
        errors = _http_gate_errors(http_gate_tree)
        assert any("direct HTTP import" in e.message for e in errors)

    def test_gate_h1_urllib_alias_form_caught(self, http_gate_tree):
        bad = http_gate_tree["SRC"] / "sneaky.py"
        bad.write_text("from urllib import request\n", encoding="utf-8")
        errors = _http_gate_errors(http_gate_tree)
        assert any("direct HTTP import" in e.message for e in errors)

    def test_gate_h1_urllib_parse_is_allowed(self, http_gate_tree):
        ok = http_gate_tree["SRC"] / "parser.py"
        ok.write_text("from urllib.parse import quote\n", encoding="utf-8")
        assert _http_gate_errors(http_gate_tree) == []

    def test_gate_h1_provider_client_is_exempt(self, http_gate_tree):
        # provider_client.py already imports requests in the healthy tree.
        assert _http_gate_errors(http_gate_tree) == []

    def test_gate_h2_flat_constant_in_discovery_caught(self, http_gate_tree):
        bad = http_gate_tree["SRC"] / "flat_user.py"
        bad.write_text(
            "from config.settings import DISCOVERY_KEYWORD_NOTEBOOK_DIR\n",
            encoding="utf-8",
        )
        errors = _http_gate_errors(http_gate_tree)
        assert any("retired flat discovery path constant" in e.message for e in errors)

    def test_gate_h2_flat_constant_in_audit_script_caught(self, http_gate_tree):
        script = http_gate_tree["SCRIPTS"] / "audit_discovery_keyword_index_sources.py"
        script.write_text(
            "from config.settings import DISCOVERY_PENDING_PAGES_DIR\n",
            encoding="utf-8",
        )
        errors = _http_gate_errors(http_gate_tree)
        assert any("retired flat discovery path constant" in e.message for e in errors)
