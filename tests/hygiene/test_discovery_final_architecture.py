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
DISCOVERY_MAINTENANCE_LOCK_PATH = DISCOVERY_MIGRATIONS_DIR / ".maintenance.lock"
previous_pointer_snapshot_path = None
raise CommitReconciliationError("x")
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

    def test_gate3_missing_maintenance_lock_caught(self, healthy_tree):
        ws = healthy_tree["SRC"] / "workspace.py"
        ws.write_text(_HEALTHY_WORKSPACE.replace('".maintenance.lock"', '"other"'),
                      encoding="utf-8")
        errors = _gate_errors(healthy_tree)
        assert any(".maintenance.lock" in e.message for e in errors)

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


# ── Frozen-seal Phase 2: dead v4 store stack tombstones ──────────────────


def _tombstone_gate_errors(dirs) -> list:
    report = verifier.VerifierReport()
    verifier._check_dead_v4_store_tombstones(report)
    return report.errors


class TestDeadV4StoreTombstones:
    """The deleted zero-reader v4 store stack must never reappear."""

    def test_healthy_tree_passes(self, healthy_tree):
        assert _tombstone_gate_errors(healthy_tree) == []

    @pytest.mark.parametrize(
        "rel",
        [
            "stores/lane_state_store.py",
            "stores/journal_index.py",
            "stores/report_store.py",
            "contracts/lane_state.py",
        ],
    )
    def test_dead_module_file_caught(self, healthy_tree, rel: str):
        dead = healthy_tree["SRC"] / rel
        dead.parent.mkdir(parents=True, exist_ok=True)
        dead.write_text("# resurrected dead store\n", encoding="utf-8")
        errors = _tombstone_gate_errors(healthy_tree)
        assert any(dead.name in e.file and "must be removed" in e.message
                   for e in errors)

    @pytest.mark.parametrize(
        "token",
        [
            "LaneStateStoreV4",
            "JournalIndexV4",
            "ReportStoreV4",
            "LaneStateV4",
            "CursorTransactionV4",
        ],
    )
    def test_dead_token_reintroduction_caught(self, healthy_tree, token: str):
        evil = healthy_tree["SRC"] / "evil.py"
        evil.write_text(f"from y import {token}\n", encoding="utf-8")
        errors = _tombstone_gate_errors(healthy_tree)
        assert any(token in e.message for e in errors)


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
            'with DiscoveryWriterLease("discover-papers"):\n'
            "    run()\n"
        ),
        (scripts / "discover_papers_concurrent.py"): (
            'with DiscoveryWriterLease("discover-papers-concurrent"):\n'
            "    run()\n"
        ),
        (src / "coordinator.py"): _HEALTHY_FINAL_COORDINATOR,
        (src / "maintenance_gate.py"): (
            "class DiscoveryWriterLease:\n"
            "    pass\n"
            "def active_writer_leases(lock_path=None):\n"
            "    return []\n"
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

    def test_gate15_writer_without_writer_lease_caught(self, healthy_final_tree):
        writer = healthy_final_tree["SCRIPTS"] / "discover_papers_concurrent.py"
        writer.write_text("def main():\n    return 0\n", encoding="utf-8")
        errors = _final_gate_errors(healthy_final_tree)
        assert any("discover_papers_concurrent.py" in e.file
                   and "DiscoveryWriterLease" in e.message
                   for e in errors)

    def test_gate15_workspace_root_bypass_caught(self, healthy_final_tree):
        writer = healthy_final_tree["SCRIPTS"] / "discover_papers.py"
        writer.write_text(
            "if not args.workspace_root:\n"
            '    DiscoveryWriterLease("discover-papers")\n',
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
        page_journals_dir=tmp_path / "workspace" / "page_journals",
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


# ── Final-freeze behavioral gates: positive + negative coverage ──────────

_BEHAVIORAL_GATE_NAMES = {
    "pointer-negative-probes",
    "manifest-negative-probes",
    "runtime-error-taxonomy",
    "workspace-identity-and-symlink",
    "bootstrap-crash-windows",
    "maintenance-exclusion",
    "server-layering",
    "lock-before-resolve-ordering",
    "test-helper-identity",
    "stale-document-commands",
}


def _behavioral_errors(*gate_fns) -> list:
    report = verifier.VerifierReport()
    for fn in gate_fns:
        fn(report)
    return report.errors


class TestFinalFreezeBehavioralGates:
    """The behavioral gates execute the production code with injected
    negative inputs; each must catch its violation and pass on the real
    repo."""

    def test_real_repo_passes_all_behavioral_gates(self):
        report = verifier.VerifierReport()
        verifier._check_final_freeze_behavioral_rules(report)
        errors = report.errors
        if errors:
            lines = [f"{len(errors)} behavioral gate violation(s):"]
            for f in errors:
                lines.append(f"  {f.file}: {f.message}")
            pytest.fail("\n".join(lines))

    def test_gate_results_cover_all_ten_gates(self):
        report = verifier.VerifierReport()
        verifier._check_final_freeze_behavioral_rules(report)
        assert _BEHAVIORAL_GATE_NAMES <= set(report.gate_results)
        for name in _BEHAVIORAL_GATE_NAMES:
            entry = report.gate_results[name]
            assert entry["probes"] > 0, f"{name} ran zero probes"

    def test_pointer_gate_catches_lenient_parser(self, monkeypatch):
        from src.discovery.contracts import manifest as manifest_mod

        monkeypatch.setattr(
            manifest_mod.ActiveGenerationPointerV4,
            "from_dict_strict",
            classmethod(lambda cls, data: data),
        )
        errors = _behavioral_errors(verifier._gate_pointer_negative_probes)
        assert any("pointer-negative-probes" in f.file for f in errors)

    def test_manifest_gate_catches_lenient_parser(self, monkeypatch):
        from src.discovery.contracts import manifest as manifest_mod

        monkeypatch.setattr(
            manifest_mod.DiscoveryWorkspaceManifestV4,
            "from_dict_strict",
            classmethod(lambda cls, data: data),
        )
        errors = _behavioral_errors(verifier._gate_manifest_negative_probes)
        assert any("manifest-negative-probes" in f.file for f in errors)

    def test_taxonomy_gate_catches_wrong_mapping(self, monkeypatch):
        from src.discovery import runtime_context as rc

        monkeypatch.setattr(
            rc,
            "_map_resolution_error",
            lambda exc, *, origin: rc.DiscoveryRuntimeNotInitialized(str(exc)),
        )
        errors = _behavioral_errors(verifier._gate_runtime_error_taxonomy)
        assert any("runtime-error-taxonomy" in f.file for f in errors)

    def test_workspace_gate_catches_lenient_resolver(self, monkeypatch):
        from types import SimpleNamespace

        from src.discovery import workspace as wsp

        monkeypatch.setattr(
            wsp.WorkspaceResolver,
            "resolve_explicit_workspace",
            staticmethod(lambda root, **kwargs: SimpleNamespace(
                generation_id="wrong-id"
            )),
        )
        errors = _behavioral_errors(
            verifier._gate_workspace_identity_and_symlink
        )
        assert any("workspace-identity-and-symlink" in f.file for f in errors)

    def test_bootstrap_gate_catches_missing_recovery(self, monkeypatch):
        import src.discovery.workspace as wsp

        monkeypatch.setattr(
            wsp,
            "bootstrap_initial_workspace",
            lambda generation_id=None: (None, True),
        )
        errors = _behavioral_errors(verifier._gate_bootstrap_crash_windows)
        assert any("bootstrap-crash-windows" in f.file for f in errors)

    def test_maintenance_gate_catches_missing_exclusion(self, monkeypatch):
        from src.discovery import maintenance_gate as mg

        monkeypatch.setattr(
            mg.DiscoveryMaintenanceLock, "acquire", lambda self: self
        )
        errors = _behavioral_errors(verifier._gate_maintenance_exclusion)
        assert any("maintenance-exclusion" in f.file for f in errors)

    def test_server_gate_catches_service_init_in_middleware(
        self, tmp_path, monkeypatch
    ):
        src_root = tmp_path / "src"
        src_root.mkdir()
        (src_root / "server.py").write_text(
            "async def security_headers_and_api_key(request, call_next):\n"
            "    _get_catalog()\n"
            "    return await call_next(request)\n"
            "def _get_catalog():\n    pass\n"
            "def _get_library():\n    pass\n"
            "def _get_prompt_builder():\n    pass\n"
            "def _get_job_manager():\n    pass\n"
            'X = "/status/discovery"\n'
            "Y = 'exception_handler(DiscoveryRuntimeUnavailableError)'\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(verifier, "SRC_ROOT", src_root)
        errors = _behavioral_errors(verifier._gate_server_layering)
        assert any("server-layering" in f.file for f in errors)

    def test_lock_order_gate_catches_resolve_before_lease(
        self, tmp_path, monkeypatch
    ):
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "discover_papers.py").write_text(
            "def main():\n"
            "    ws = resolve_active()\n"
            '    with DiscoveryWriterLease("x"):\n'
            "        return _run(args)\n",
            encoding="utf-8",
        )
        (scripts / "discover_papers_concurrent.py").write_text(
            "def main_internal(argv):\n"
            '    with DiscoveryWriterLease("x"):\n'
            "        return _run(args)\n"
            "def _run(args):\n"
            "    ws = resolve_active()\n",
            encoding="utf-8",
        )
        (scripts / "manage_discovery_keywords.py").write_text(
            "lock = DiscoveryMaintenanceLock(purpose)\n"
            "ctx = resolve_active_runtime(workspace_root=None)\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(verifier, "SCRIPTS", scripts)
        errors = _behavioral_errors(verifier._gate_lock_before_resolve)
        assert any("lock-before-resolve-ordering" in f.file for f in errors)
        assert any("discover_papers.py" in f.message for f in errors)

    def test_lock_order_gate_catches_manage_resolve_before_lock(
        self, tmp_path, monkeypatch
    ):
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        healthy = (
            "def main(argv):\n"
            '    with DiscoveryWriterLease("x"):\n'
            "        return _run(args)\n"
            "def _run(args):\n"
            "    ws = resolve_active()\n"
        )
        (scripts / "discover_papers.py").write_text(healthy, encoding="utf-8")
        (scripts / "discover_papers_concurrent.py").write_text(
            healthy.replace("def main(", "def main_internal("),
            encoding="utf-8",
        )
        (scripts / "manage_discovery_keywords.py").write_text(
            "ctx = resolve_active_runtime(workspace_root=None)\n"
            "lock = DiscoveryMaintenanceLock(purpose)\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(verifier, "SCRIPTS", scripts)
        errors = _behavioral_errors(verifier._gate_lock_before_resolve)
        assert any("manage_discovery_keywords" in f.message for f in errors)

    def test_stale_docs_gate_catches_token_outside_adr(
        self, tmp_path, monkeypatch
    ):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "GUIDE.md").write_text(
            "run scripts/migrate_discovery_v4.py --finalize\n", encoding="utf-8"
        )
        (docs / "ADR_DISCOVERY_V4_MIGRATION_FINAL.md").write_text(
            "historical: PendingCandidateStoreV4 removed\n", encoding="utf-8"
        )
        monkeypatch.setattr(verifier, "DOCS_DIR", docs)
        monkeypatch.setattr(verifier, "PROJECT_ROOT", tmp_path)
        errors = _behavioral_errors(verifier._gate_stale_document_commands)
        assert any("migrate_discovery_v4" in f.message for f in errors)
        assert not any("ADR_DISCOVERY_V4_MIGRATION_FINAL" in f.message
                       for f in errors)

    def test_stale_docs_gate_passes_with_adr_only_tokens(
        self, tmp_path, monkeypatch
    ):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "ADR_DISCOVERY_V4_SINGLE_STACK.md").write_text(
            "historical: migrate_discovery_v4 --clean-legacy\n", encoding="utf-8"
        )
        (docs / "CLEAN.md").write_text("no stale tokens here\n",
                                       encoding="utf-8")
        monkeypatch.setattr(verifier, "DOCS_DIR", docs)
        monkeypatch.setattr(verifier, "PROJECT_ROOT", tmp_path)
        assert _behavioral_errors(verifier._gate_stale_document_commands) == []

    def test_helper_gate_catches_unresolvable_fixture(self, monkeypatch):
        from types import SimpleNamespace

        from tests.helpers import discovery_workspace as helper_mod

        def bad_make(root):
            root = Path(root)
            root.mkdir(parents=True, exist_ok=True)
            return SimpleNamespace(generation_id=root.name, root=root)

        monkeypatch.setattr(helper_mod, "make_test_workspace", bad_make)
        errors = _behavioral_errors(verifier._gate_test_helper_identity)
        assert any("test-helper-identity" in f.file for f in errors)

    def test_helper_gate_catches_wrong_generation_identity(self, monkeypatch):
        from types import SimpleNamespace

        from tests.helpers import discovery_workspace as helper_mod

        real_make = helper_mod.make_test_workspace

        def wrong_id_make(root):
            real_make(root)  # build a fully resolvable fixture
            return SimpleNamespace(generation_id="other-id", root=Path(root))

        monkeypatch.setattr(helper_mod, "make_test_workspace", wrong_id_make)
        errors = _behavioral_errors(verifier._gate_test_helper_identity)
        assert any("test-helper-identity" in f.file for f in errors)

    def test_helper_gate_marks_legacy_prefix_transitional(self, monkeypatch):
        from types import SimpleNamespace

        from tests.helpers import discovery_workspace as helper_mod

        real_make = helper_mod.make_test_workspace

        def legacy_make(root):
            real_make(root)  # resolvable fixture, old identity convention
            return SimpleNamespace(
                generation_id=f"test-{Path(root).name}", root=Path(root)
            )

        monkeypatch.setattr(helper_mod, "make_test_workspace", legacy_make)
        report = verifier.VerifierReport()
        verifier._gate_test_helper_identity(report)
        assert report.errors == []
        entry = report.gate_results["test-helper-identity"]
        assert entry["passed"] is True
        assert any("transitional" in w for w in entry["warnings"])
