#!/usr/bin/env python
"""Verify discovery final architecture via AST inspection.

Runs as a standalone script or via pytest hygiene test.
Exits 0 when all checks pass; exits 1 with itemized failures otherwise.

Usage:
  python scripts/verify_discovery_final_architecture.py           # check all
  python scripts/verify_discovery_final_architecture.py --json    # JSON output
"""

from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
SRC = SRC_ROOT / "discovery"
SCRIPTS = PROJECT_ROOT / "scripts"


@dataclass
class Finding:
    level: str  # always "error"; architecture drift is fail-closed
    category: str  # "forbidden" | "missing_required" | "call_graph"
    file: str
    line: int
    message: str


@dataclass
class VerifierReport:
    findings: list[Finding] = field(default_factory=list)
    files_scanned: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return []

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "files_scanned": len(self.files_scanned),
            "findings": [
                {"level": f.level, "category": f.category,
                 "file": f.file, "line": f.line, "message": f.message}
                for f in self.findings
            ],
        }


# ── Helpers ─────────────────────────────────────────────────────────────


def _scan_file(path: Path, report: VerifierReport, filepath: str) -> ast.AST | None:
    """Parse a Python file; syntax errors are recorded as fatal findings."""
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        report.findings.append(Finding(
            level="error", category="forbidden",
            file=filepath, line=getattr(exc, "lineno", 0),
            message=f"syntax error: {exc.msg}",
        ))
        return None


def _is_nested_function(node: ast.FunctionDef, parent_depth: int = 0) -> bool:
    """Check if a function definition is nested (not top-level)."""
    # We detect nesting by looking at the tree structure — the verifier
    # uses the visitor pattern to track depth.
    return parent_depth > 0


class _ForbiddenVisitor(ast.NodeVisitor):
    """Collect findings for forbidden production symbols."""

    def __init__(self, filepath: str, report: VerifierReport):
        self.filepath = filepath
        self.report = report
        self._depth = 0
        self._imported_names: dict[str, str] = {}  # local_name -> module
        self._function_stack: list[str] = []

    def _error(self, category: str, line: int, message: str) -> None:
        self.report.findings.append(
            Finding(level="error", category=category, file=self.filepath,
                    line=line, message=message)
        )

    def _warn(self, category: str, line: int, message: str) -> None:
        self._error(category, line, message)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname or alias.name
            self._imported_names[name] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            name = alias.asname or alias.name
            self._imported_names[name] = f"{module}.{alias.name}"

            # ── v4 single-stack: the retired top-level alias shells
            # (keyword_notebook.py / page_journal.py) are deleted; any import
            # from those module paths is a hard failure.  The post-scan rule
            # in _check_single_stack_rules additionally asserts the files do
            # not exist at all.
            if module in {"src.discovery.keyword_notebook", "src.discovery.page_journal"}:
                self._error(
                    "forbidden", node.lineno,
                    f"import from retired alias shell {module} — "
                    "use src.discovery.contracts.* / src.discovery.stores.*",
                )
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        is_nested = self._depth > 0
        argument_names_ordered = [
            arg.arg for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        ]

        if "coordinator" in self.filepath:
            argument_names = set(argument_names_ordered)
            if node.args.vararg is not None:
                argument_names.add(node.args.vararg.arg)
            if node.args.kwarg is not None:
                argument_names.add(node.args.kwarg.arg)
            retired = argument_names & {"fetch_page", "rate_limiters"}
            if retired:
                self._error(
                    "forbidden", node.lineno,
                    "coordinator exposes retired loose arguments " + ", ".join(sorted(retired)),
                )

        # A production page adapter may only accept the immutable execution
        # spec, cursor, and batch-bound client.  Loose query/filter/rate
        # arguments recreate the retired dual path even if the coordinator is
        # otherwise typed.
        page_adapter_files = {
            "src/discovery/search_openalex.py",
            "src/discovery/resolve_crossref.py",
        }
        if self.filepath in page_adapter_files and node.name in {
            "search_openalex_page", "search_crossref_page",
        }:
            if argument_names_ordered != ["lane_spec", "cursor", "client"]:
                self._error(
                    "forbidden", node.lineno,
                    f"{node.name} must accept exactly (lane_spec, cursor, client)",
                )

        if self.filepath == "src/discovery/provider_page_fetcher.py" and node.name == "fetch":
            if argument_names_ordered != ["self", "spec", "cursor", "client"]:
                self._error(
                    "forbidden", node.lineno,
                    "ProviderPageFetcher.fetch must accept exactly (self, spec, cursor, client)",
                )

        # ── Forbidden: run_refresh as nested function in coordinator ──
        if node.name == "run_refresh" and is_nested and "coordinator" in self.filepath:
            self._error(
                "forbidden", node.lineno,
                f"nested function 'run_refresh' in coordinator — must use execute_refresh_lane"
            )

        # ── Forbidden: run_backfill as nested function in coordinator ──
        if node.name == "run_backfill" and is_nested and "coordinator" in self.filepath:
            self._error(
                "forbidden", node.lineno,
                f"nested function 'run_backfill' in coordinator — must use execute_backfill_lane"
            )

        # ── Forbidden: _build_aggregate in coordinator ──
        if node.name == "_build_aggregate" and "coordinator" in self.filepath:
            self._error(
                "forbidden", node.lineno,
                "function '_build_aggregate' in coordinator — aggregation must be in ReportBuilder"
            )

        # ── Forbidden: advance_backfill on KeywordNotebookStore ──
        if node.name == "advance_backfill" and "keyword_notebook" in self.filepath:
            self._error(
                "forbidden", node.lineno,
                "method 'advance_backfill' on KeywordNotebookStore — must be deleted"
            )

        # ── Forbidden: set_request_budget method ──
        if node.name == "set_request_budget" and "provider_client" in self.filepath:
            self._error(
                "forbidden", node.lineno,
                "method 'set_request_budget' on ProviderRuntime — must be deleted"
            )

        # ── Forbidden: _lane_report_from_outcome in coordinator ──
        if node.name == "_lane_report_from_outcome" and "coordinator" in self.filepath:
            self._error(
                "forbidden", node.lineno,
                "function '_lane_report_from_outcome' in coordinator — LaneOutcome must go directly to ReportBuilder"
            )

        # ── Forbidden: _merge_lane_report in coordinator ──
        if node.name == "_merge_lane_report" and "coordinator" in self.filepath:
            self._error(
                "forbidden", node.lineno,
                "function '_merge_lane_report' in coordinator — lane merging must be in ReportBuilder"
            )

        # ── Forbidden: _execute_refresh_for_keyword in coordinator ──
        if node.name == "_execute_refresh_for_keyword" and "coordinator" in self.filepath:
            self._error(
                "forbidden", node.lineno,
                "function '_execute_refresh_for_keyword' in coordinator — must use per-lane futures"
            )

        # ── Forbidden: _execute_backfill_for_keyword in coordinator ──
        if node.name == "_execute_backfill_for_keyword" and "coordinator" in self.filepath:
            self._error(
                "forbidden", node.lineno,
                "function '_execute_backfill_for_keyword' in coordinator — must use per-lane futures"
            )

        # ── Forbidden: fetch_with_budget in coordinator ──
        if node.name == "fetch_with_budget" and "coordinator" in self.filepath:
            self._error(
                "forbidden", node.lineno,
                "function 'fetch_with_budget' in coordinator — must be deleted; budget belongs in runtime"
            )

        self._depth += 1
        self._function_stack.append(node.name)
        self.generic_visit(node)
        self._function_stack.pop()
        self._depth -= 1

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    def visit_Assign(self, node: ast.Assign) -> None:
        # ── Forbidden: provider_runtime.telemetry = ... ──
        for target in node.targets:
            if isinstance(target, ast.Attribute):
                if (
                    isinstance(target.value, ast.Name)
                    and target.value.id == "provider_runtime"
                    and target.attr == "telemetry"
                ):
                    self._error(
                        "forbidden", node.lineno,
                        "provider_runtime.telemetry batch mutation — telemetry must be batch-scoped"
                    )

        # ── Forbidden: _provider_locks = {} in lane_executor ──
        if "lane_executor" in self.filepath:
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_provider_locks":
                    self._error(
                        "forbidden", node.lineno,
                        "local '_provider_locks' dict in lane_executor — locks must be injected by caller"
                    )

        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        # ── Forbidden: _provider_locks with annotation in lane_executor ──
        if "lane_executor" in self.filepath:
            if isinstance(node.target, ast.Name) and node.target.id == "_provider_locks":
                self._error(
                    "forbidden", node.lineno,
                    "local '_provider_locks' dict in lane_executor — locks must be injected by caller"
                )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # Check for LaneMachine constructions with string-formatted lane keys
        if isinstance(node.func, ast.Name) and node.func.id == "LaneMachine":
            for arg in node.args + [kw.value for kw in node.keywords]:
                if isinstance(arg, ast.JoinedStr):
                    for value in arg.values:
                        if isinstance(value, ast.Constant) and isinstance(value.value, str):
                            if ":refresh" in value.value or ":backfill" in value.value:
                                self._error(
                                    "forbidden", node.lineno,
                                    "LaneMachine constructed with string-formatted lane key "
                                    f"('{value.value[:60]}...') — use DiscoveryLaneKey"
                                )

        # Check for SimpleNamespace usage
        if isinstance(node.func, ast.Name) and node.func.id == "SimpleNamespace":
            if "backfill_transaction" in self.filepath:
                self._error(
                    "forbidden", node.lineno,
                    "SimpleNamespace usage in backfill_transaction — use DurableProviderPage"
                )

        # ── Forbidden: ProviderRuntime.get().client() in coordinator ──
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "client"
            and isinstance(node.func.value, ast.Call)
            and isinstance(node.func.value.func, ast.Attribute)
            and node.func.value.func.attr == "get"
            and isinstance(node.func.value.func.value, ast.Name)
            and node.func.value.func.value.id == "ProviderRuntime"
        ):
            if "coordinator" in self.filepath:
                self._error(
                    "forbidden", node.lineno,
                    "ProviderRuntime.get().client() in coordinator — must use runtime.provider_client()"
                )

            if (
                self.filepath in {
                    "src/discovery/search_openalex.py",
                    "src/discovery/resolve_crossref.py",
                }
                and self._function_stack
                and self._function_stack[-1] in {
                    "search_openalex_page", "search_crossref_page",
                }
            ):
                self._error(
                    "forbidden", node.lineno,
                    "batch page adapter must use its injected ProviderClient, never ProviderRuntime.get().client()",
                )

        # ── Forbidden: LaneReport() construction outside report_builder ──
        if isinstance(node.func, ast.Name) and node.func.id == "LaneReport":
            if "report_builder" not in self.filepath:
                self._error(
                    "forbidden", node.lineno,
                    "LaneReport construction outside report_builder — use make_lane_report()"
                )

        if "coordinator" in self.filepath:
            if isinstance(node.func, ast.Name) and node.func.id in {
                "lane_report_from_outcome", "merge_lane_report", "make_lane_report",
                "build_aggregate", "_default_fetch_page",
            }:
                self._error(
                    "forbidden", node.lineno,
                    f"coordinator calls retired report/fetch path {node.func.id}",
                )
            for keyword in node.keywords:
                if keyword.arg in {"fetch_page", "rate_limiters"}:
                    self._error(
                        "forbidden", node.lineno,
                        f"coordinator exposes retired loose argument {keyword.arg}",
                    )

        self.generic_visit(node)


class _RequiredCallsVisitor(ast.NodeVisitor):
    """Collect findings for required production calls that must exist."""

    def __init__(self, filepath: str, report: VerifierReport):
        self.filepath = filepath
        self.report = report
        self._found_required: set[str] = set()
        self._imported_names: set[str] = set()

    def _error(self, line: int, message: str) -> None:
        self.report.findings.append(
            Finding(level="error", category="missing_required",
                    file=self.filepath, line=line, message=message)
        )

    def _warn(self, line: int, message: str) -> None:
        self._error(line, message)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.name == "begin_refresh":
            self._found_required.add("begin_refresh")
        if node.name == "complete_refresh":
            self._found_required.add("complete_refresh")
        if node.name == "build_batch_report":
            self._found_required.add("build_batch_report")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self._imported_names.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # execute_refresh_lane call
        if isinstance(node.func, ast.Name) and node.func.id == "execute_refresh_lane":
            self._found_required.add("execute_refresh_lane")
        # execute_backfill_lane call
        if isinstance(node.func, ast.Name) and node.func.id == "execute_backfill_lane":
            self._found_required.add("execute_backfill_lane")
        if isinstance(node.func, ast.Name) and node.func.id == "LaneExecutionSpec":
            self._found_required.add("LaneExecutionSpec")
        # DurableProviderPage construction (direct or via from_journal)
        if isinstance(node.func, ast.Name) and node.func.id == "DurableProviderPage":
            self._found_required.add("DurableProviderPage")
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in ("from_journal",)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "DurableProviderPage"
        ):
            self._found_required.add("DurableProviderPage")
            self._found_required.add("DurableProviderPage.from_journal")
        # GenerationHistoryEntry.from_dict_strict call
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "from_dict_strict"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "GenerationHistoryEntry"
        ):
            self._found_required.add("GenerationHistoryEntry.from_dict_strict")
        # DualScopePageBudget.try_acquire / page_budget.try_acquire call
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "try_acquire"
        ):
            self._found_required.add("page_budget.try_acquire")
        # provider_client call on runtime
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "provider_client"
        ):
            self._found_required.add("provider_client")
        # begin_refresh / complete_refresh calls
        if isinstance(node.func, ast.Attribute) and node.func.attr == "begin_refresh":
            self._found_required.add("begin_refresh")
        if isinstance(node.func, ast.Attribute) and node.func.attr == "complete_refresh":
            self._found_required.add("complete_refresh")
        if isinstance(node.func, ast.Attribute) and node.func.attr == "fetch":
            self._found_required.add("ProviderPageFetcher.fetch")
        if isinstance(node.func, ast.Attribute) and node.func.attr == "build":
            self._found_required.add("ReportBuilder.build")
        # build_batch_report call
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "build_batch_report"
        ):
            self._found_required.add("build_batch_report")
        # lane_report_from_outcome call (should be from report_builder, not coordinator)
        if isinstance(node.func, ast.Name) and node.func.id == "lane_report_from_outcome":
            self._found_required.add("lane_report_from_outcome")

        self.generic_visit(node)

    def check_file_requirements(self) -> None:
        """Post-visit: enforce file-specific required calls."""
        fp = self.filepath

        # Coordinator must call execute_refresh_lane and execute_backfill_lane
        if "coordinator" in fp:
            if "execute_refresh_lane" not in self._found_required:
                self._error(0, "coordinator must call execute_refresh_lane")
            if "execute_backfill_lane" not in self._found_required:
                self._error(0, "coordinator must call execute_backfill_lane")
            if "LaneExecutionSpec" not in self._found_required:
                self._error(0, "coordinator must construct immutable LaneExecutionSpec values")
            if "ReportBuilder.build" not in self._found_required:
                self._error(0, "coordinator must directly invoke ReportBuilder.build")

        # lane_executor should call begin_refresh / complete_refresh
        # (Phase 8: requires refresh state service integration)
        if "lane_executor" in fp:
            if "begin_refresh" not in self._found_required:
                self._error(0, "lane_executor must call begin_refresh for refresh state lifecycle")
            if "complete_refresh" not in self._found_required:
                self._error(0, "lane_executor must call complete_refresh for refresh state lifecycle")
            if "page_budget.try_acquire" not in self._found_required:
                self._error(0, "lane_executor must call page_budget.try_acquire for global page budget")
            if "ProviderPageFetcher.fetch" not in self._found_required:
                self._error(0, "lane_executor must call ProviderPageFetcher.fetch")

        # backfill_transaction must construct DurableProviderPage
        if "backfill_transaction" in fp:
            if "DurableProviderPage" not in self._found_required:
                self._error(0, "backfill_transaction must construct DurableProviderPage")
            if "DurableProviderPage.from_journal" not in self._found_required:
                self._error(0, "backfill_transaction must use DurableProviderPage.from_journal for recovery")

        # contracts/notebook must use GenerationHistoryEntry.from_dict_strict
        if "contracts/notebook" in fp:
            if "GenerationHistoryEntry.from_dict_strict" not in self._found_required:
                self._error(0, "contracts/notebook must use GenerationHistoryEntry.from_dict_strict for strict validation")

        # report_builder should define and use build_batch_report
        if "report_builder" in fp:
            if "build_batch_report" not in self._found_required:
                self._error(0, "report_builder must expose build_batch_report for coordinator aggregation")


class _CallGraphVisitor(ast.NodeVisitor):
    """Verify call graph: run_discovery_batch → DiscoveryBatchRuntime
    → execute_refresh_lane/execute_backfill_lane → LaneOutcome → ReportBuilder."""

    def __init__(self, filepath: str, report: VerifierReport):
        self.filepath = filepath
        self.report = report
        self._coordinator_has_discovery_lane_key = False
        self._coordinator_has_lane_outcome = False
        self._coordinator_imports_executor = False
        self._coordinator_imports_report_builder = False
        self._coordinator_imports_spec = False
        self._coordinator_calls_builder = False

    def _error(self, line: int, message: str) -> None:
        self.report.findings.append(
            Finding(level="error", category="call_graph",
                    file=self.filepath, line=line, message=message)
        )

    def _warn(self, line: int, message: str) -> None:
        self._error(line, message)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        names = {alias.name for alias in node.names}

        if "coordinator" in self.filepath:
            if "lane_executor" in module:
                if {"execute_refresh_lane", "execute_backfill_lane"} & names:
                    self._coordinator_imports_executor = True
            if "report_builder" in module:
                self._coordinator_imports_report_builder = self._coordinator_imports_report_builder or ("ReportBuilder" in names)
            if "lane_models" in module:
                if "LaneOutcome" in names:
                    self._coordinator_has_lane_outcome = True
                if "DiscoveryLaneKey" in names:
                    self._coordinator_has_discovery_lane_key = True
                if "LaneExecutionSpec" in names and "RequestSignature" in names:
                    self._coordinator_imports_spec = True

        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if "coordinator" in self.filepath and (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "build"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "builder"
        ):
            self._coordinator_calls_builder = True
        self.generic_visit(node)

    def check_file_requirements(self) -> None:
        """Post-visit: enforce file-specific call-graph requirements."""
        fp = self.filepath

        if "coordinator" in fp:
            if not self._coordinator_imports_executor:
                self._error(0, "coordinator must import from lane_executor (execute_refresh_lane, execute_backfill_lane)")
            if not self._coordinator_imports_report_builder:
                self._error(0, "coordinator must import from report_builder for aggregation")
            if not self._coordinator_has_lane_outcome:
                self._error(0, "coordinator must import LaneOutcome from lane_models")
            if not self._coordinator_has_discovery_lane_key:
                self._error(0, "coordinator must import DiscoveryLaneKey from lane_models")
            if not self._coordinator_imports_spec:
                self._error(0, "coordinator must import RequestSignature and LaneExecutionSpec")
            if not self._coordinator_calls_builder:
                self._error(0, "coordinator must call ReportBuilder.build")


def verify_file(path: Path, report: VerifierReport) -> None:
    """Run all verifier checks on a single Python file."""
    if not path.suffix == ".py":
        return
    if path.name.startswith("__"):
        return

    filepath = str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    tree = _scan_file(path, report, filepath)
    if tree is None:
        return

    report.files_scanned.append(filepath)

    # 1. Forbidden patterns
    forbidden = _ForbiddenVisitor(filepath, report)
    forbidden.visit(tree)

    # 2. Required calls
    required = _RequiredCallsVisitor(filepath, report)
    required.visit(tree)
    required.check_file_requirements()

    # 3. Call graph
    callgraph = _CallGraphVisitor(filepath, report)
    callgraph.visit(tree)
    callgraph.check_file_requirements()


def _check_phase7_rules(report: VerifierReport) -> None:
    """Post-scan checks for Phase 7 architectural invariants."""
    # 1. batch_runtime.py must have __enter__, __exit__, cancellation_token, closed_event, _frozen
    br_path = SRC / "runtime" / "batch_runtime.py"
    if br_path.exists():
        br_text = br_path.read_text(encoding="utf-8")
        required = ["def __enter__", "def __exit__",
                    "cancellation_token", "closed_event", "_frozen"]
        for token in required:
            if token not in br_text:
                report.findings.append(Finding(
                    level="error", category="missing_required",
                    file="src/discovery/batch_runtime.py", line=0,
                    message=f"DiscoveryBatchRuntime must contain {token!r}",
                ))

    # 2. provider_client.py must NOT have request_budget on ProviderRuntime.__init__
    pc_path = SRC / "providers" / "provider_client.py"
    if pc_path.exists():
        pc_text = pc_path.read_text(encoding="utf-8")
        # Find ProviderRuntime class and its __init__ specifically
        lines = pc_text.splitlines()
        in_provider_runtime = False
        in_pr_init = False
        for line in lines:
            if "class ProviderRuntime" in line:
                in_provider_runtime = True
                continue
            if in_provider_runtime and line.strip().startswith("class "):
                in_provider_runtime = False
                in_pr_init = False
            if in_provider_runtime and "def __init__" in line:
                in_pr_init = True
                continue
            if in_provider_runtime and in_pr_init and line.strip().startswith("def "):
                in_pr_init = False
                continue
            if in_pr_init and "request_budget" in line:
                report.findings.append(Finding(
                    level="error", category="forbidden",
                    file="src/discovery/provider_client.py", line=0,
                    message="ProviderRuntime.__init__ must not have request_budget parameter",
                ))
                break

    # 3. pending_queue.py DrainOutcome must have >= 6 members
    pq_path = SRC / "pending_queue.py"
    if pq_path.exists():
        pq_text = pq_path.read_text(encoding="utf-8")
        if "class DrainOutcome" in pq_text:
            # Count enum members by counting lines with "= " after class DrainOutcome
            in_enum = False
            member_count = 0
            for line in pq_text.splitlines():
                stripped = line.strip()
                if stripped.startswith("class DrainOutcome"):
                    in_enum = True
                    continue
                if in_enum and stripped.startswith("class "):
                    break
                if in_enum and "=" in stripped and not stripped.startswith("#"):
                    member_count += 1
            if member_count < 6:
                report.findings.append(Finding(
                    level="error", category="missing_required",
                    file="src/discovery/pending_queue.py", line=0,
                    message=f"DrainOutcome must have >= 6 members, got {member_count}",
                ))

    # 4. provider_telemetry.py must define TelemetryScope and TelemetryCounters
    pt_path = SRC / "providers" / "provider_telemetry.py"
    if pt_path.exists():
        pt_text = pt_path.read_text(encoding="utf-8")
        for cls_name in ("TelemetryScope", "TelemetryCounters"):
            if f"class {cls_name}" not in pt_text:
                report.findings.append(Finding(
                    level="error", category="missing_required",
                    file="src/discovery/provider_telemetry.py", line=0,
                    message=f"provider_telemetry.py must define {cls_name}",
                ))

    # 5. report_builder.py must have planned_lane_ids parameter in build()
    rb_path = SRC / "reporting" / "report_builder.py"
    if rb_path.exists():
        rb_text = rb_path.read_text(encoding="utf-8")
        if "planned_lane_ids" not in rb_text:
            report.findings.append(Finding(
                level="error", category="missing_required",
                file="src/discovery/report_builder.py", line=0,
                message="ReportBuilder.build must accept planned_lane_ids parameter",
            ))
        # Check that backpressure blanking is guarded by 'not refresh_outcomes'
        if "item.backpressure" in rb_text:
            # Find the backpressure block
            if "if item.backpressure and not refresh_outcomes" not in rb_text:
                report.findings.append(Finding(
                    level="error", category="forbidden",
                    file="src/discovery/report_builder.py", line=0,
                    message="backpressure must NOT unconditionally blank lanes (must check 'not refresh_outcomes')",
                ))

    # 6. provider_client.py must NOT call breaker.record_failure on 429
    if pc_path.exists():
        pc_text = pc_path.read_text(encoding="utf-8")
        # Check that the 429 guard exists
        if "status_code != 429" not in pc_text and "not isinstance(last_error, ProviderRateLimited)" not in pc_text:
            report.findings.append(Finding(
                level="error", category="forbidden",
                file="src/discovery/provider_client.py", line=0,
                message="429 must NOT call breaker.record_failure; missing status_code != 429 guard",
            ))

    # ── v100 Phase 2: coordinator must use `with CandidateDrainCoordinator` ──
    coord_path = SRC / "coordinator.py"
    if coord_path.exists():
        coord_text = coord_path.read_text(encoding="utf-8")
        if "with CandidateDrainCoordinator(" not in coord_text:
            report.findings.append(Finding(
                level="error", category="missing_required",
                file="src/discovery/coordinator.py", line=0,
                message="coordinator must use `with CandidateDrainCoordinator(...) as drain:`",
            ))
        if "def safe_drain" in coord_text:
            report.findings.append(Finding(
                level="error", category="forbidden",
                file="src/discovery/coordinator.py", line=0,
                message="safe_drain wrapper must be deleted — use drain.drain() directly",
            ))

    # ── v100 Phase 3: execution/lane_scheduler.py must exist ──
    ls_path = SRC / "execution" / "lane_scheduler.py"
    if ls_path.exists():
        ls_text = ls_path.read_text(encoding="utf-8")
        if "def schedule_lanes" not in ls_text:
            report.findings.append(Finding(
                level="error", category="missing_required",
                file="src/discovery/execution/lane_scheduler.py", line=0,
                message="lane_scheduler.py must define schedule_lanes()",
            ))
    else:
        report.findings.append(Finding(
            level="error", category="missing_required",
            file="src/discovery/execution/lane_scheduler.py", line=0,
            message="execution/lane_scheduler.py is missing — scheduler must be extracted from coordinator",
        ))

    # ── v100 Phase 4: no 'unknown' batch_id fallback ──
    if pc_path.exists():
        pc_text = pc_path.read_text(encoding="utf-8")
        if 'tags.get("batch_id", "unknown")' in pc_text:
            report.findings.append(Finding(
                level="error", category="forbidden",
                file="src/discovery/provider_client.py", line=0,
                message="batch_id='unknown' fallback must be removed — use standalone:<uuid>",
            ))
        if 'tags.get("lane")' in pc_text:
            report.findings.append(Finding(
                level="error", category="forbidden",
                file="src/discovery/provider_client.py", line=0,
                message="lane fallback tags.get('lane') must be removed",
            ))

    # ── v100 Phase 4: TelemetryScope must have operation_id ──
    pt_path = SRC / "providers" / "provider_telemetry.py"
    if pt_path.exists():
        pt_text = pt_path.read_text(encoding="utf-8")
        if "operation_id" not in pt_text:
            report.findings.append(Finding(
                level="error", category="missing_required",
                file="src/discovery/provider_telemetry.py", line=0,
                message="TelemetryScope must define operation_id field",
            ))

    # ── v100 Phase 5: KeywordDiscoveryReport must have durable_progress ──
    rb_path = SRC / "reporting" / "report_builder.py"
    if rb_path.exists():
        rb_text = rb_path.read_text(encoding="utf-8")
        if "durable_progress: bool" not in rb_text:
            report.findings.append(Finding(
                level="error", category="missing_required",
                file="src/discovery/reporting/report_builder.py", line=0,
                message="KeywordDiscoveryReport must have durable_progress: bool field",
            ))


def _check_single_stack_rules(report: VerifierReport) -> None:
    """Post-scan checks for the v4 single-stack migration invariants."""

    # 1. coordinator.py must import DiscoveryStoreBundleV4 from the canonical
    #    store bundle module.
    coord_path = SRC / "coordinator.py"
    if coord_path.exists():
        coord_text = coord_path.read_text(encoding="utf-8")
        if "DiscoveryStoreBundleV4" not in coord_text:
            report.findings.append(Finding(
                level="error", category="missing_required",
                file="src/discovery/coordinator.py", line=0,
                message="coordinator must import DiscoveryStoreBundleV4",
            ))
        if "from src.discovery.stores.bundle" not in coord_text:
            report.findings.append(Finding(
                level="error", category="missing_required",
                file="src/discovery/coordinator.py", line=0,
                message="coordinator must import DiscoveryStoreBundleV4 from src.discovery.stores.bundle",
            ))

    # 2. CandidateDrainCoordinator must not accept DiscoveryOptions.
    cd_path = SRC / "runtime" / "candidate_drain.py"
    if cd_path.exists():
        cd_text = cd_path.read_text(encoding="utf-8")
        if "DiscoveryOptions" in cd_text:
            report.findings.append(Finding(
                level="error", category="forbidden",
                file="src/discovery/runtime/candidate_drain.py", line=0,
                message="CandidateDrainCoordinator must not accept DiscoveryOptions",
            ))

    # 3. No retired v3 protocol symbols in production source.
    for pyfile in sorted(SRC.rglob("*.py")):
        if pyfile.name.startswith("__"):
            continue
        fp = str(pyfile.relative_to(PROJECT_ROOT)).replace("\\", "/")
        text = pyfile.read_text(encoding="utf-8")
        if "require_v3" in text:
            report.findings.append(Finding(
                level="error", category="forbidden",
                file=fp, line=0,
                message="retired symbol 'require_v3' found in production source",
            ))
        if "PAGE_V3_FIELDS" in text:
            report.findings.append(Finding(
                level="error", category="forbidden",
                file=fp, line=0,
                message="retired symbol 'PAGE_V3_FIELDS' found in production source",
            ))
        if 'schema_version = "3.0"' in text or "schema_version = '3.0'" in text:
            report.findings.append(Finding(
                level="error", category="forbidden",
                file=fp, line=0,
                message="retired schema_version '3.0' literal found in production source",
            ))

    # 4. report_builder must emit v4 schema version.
    rb_path = SRC / "reporting" / "report_builder.py"
    if rb_path.exists():
        rb_text = rb_path.read_text(encoding="utf-8")
        if 'REPORT_SCHEMA_VERSION = "4.0"' not in rb_text:
            report.findings.append(Finding(
                level="error", category="missing_required",
                file="src/discovery/reporting/report_builder.py", line=0,
                message="report_builder must emit schema_version '4.0' (REPORT_SCHEMA_VERSION = \"4.0\")",
            ))

    # 5. No duplicate legacy module copies or retired alias shells left at the
    #    top level.
    for dead_path in [
        SRC / "batch_runtime.py",
        SRC / "provider_models.py",
        SRC / "keyword_notebook.py",
        SRC / "page_journal.py",
    ]:
        if dead_path.exists():
            report.findings.append(Finding(
                level="error", category="forbidden",
                file=str(dead_path.relative_to(PROJECT_ROOT)).replace("\\", "/"), line=0,
                message=f"duplicate legacy module {dead_path.name} must be removed",
            ))


def _require(
    report: VerifierReport,
    file_label: str,
    ok: bool,
    message: str,
    *,
    category: str = "missing_required",
) -> None:
    """Append a fail-closed finding when a structural gate is violated."""
    if not ok:
        report.findings.append(Finding(
            level="error", category=category, file=file_label, line=0,
            message=message,
        ))


def _file_label(pyfile: Path) -> str:
    """Repo-relative POSIX label; falls back to the raw path outside the repo."""
    try:
        return str(pyfile.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(pyfile).replace("\\", "/")


def _check_migration_hardening_rules(report: VerifierReport) -> None:
    """Structural gates that outlive the retired v3->v4 migration.

    These complement ``_check_single_stack_rules``: they pin the crash-safe
    generation-cutover contract in workspace.py and the retired-token
    reintroduction tombstones so regressions fail closed.  The one-time
    v3->v4 migration toolchain is deleted; no gate here may require it to
    exist.
    """
    ws_path = SRC / "workspace.py"
    ws_rel = "src/discovery/workspace.py"
    ws_text = ws_path.read_text(encoding="utf-8") if ws_path.exists() else ""
    _require(report, ws_rel, ws_path.exists(),
             "src/discovery/workspace.py is missing")

    # ── Gate 1: retired alias shells gone; no old module-path imports
    #    anywhere under src/ or scripts/.  (File non-existence is already
    #    enforced by _check_single_stack_rules rule 5; this is the import
    #    guard across the full src/scripts tree.)
    verifier_self = (SCRIPTS / "verify_discovery_final_architecture.py").resolve()
    for base in (SRC_ROOT, SCRIPTS):
        if not base.is_dir():
            continue
        for pyfile in sorted(base.rglob("*.py")):
            if pyfile.resolve() == verifier_self:
                continue
            fp = _file_label(pyfile)
            text = pyfile.read_text(encoding="utf-8")
            for retired_module in (
                "src.discovery.keyword_notebook",
                "src.discovery.page_journal",
            ):
                if retired_module in text:
                    report.findings.append(Finding(
                        level="error", category="forbidden",
                        file=fp, line=0,
                        message=f"reference to retired module path {retired_module} — "
                                "use src.discovery.contracts.* / src.discovery.stores.*",
                    ))

    # ── Gate 3: cutover is lock-guarded, snapshots the previous pointer,
    #    and reconciles crashed attempts; the pointer records the previous
    #    generation.
    _require(report, ws_rel,
             "FileLock" in ws_text and ".migration.lock" in ws_text,
             "commit_workspace must acquire the .migration.lock FileLock")
    _require(report, ws_rel,
             "previous_pointer_snapshot" in ws_text,
             "commit_workspace must snapshot the superseded previous pointer")
    _require(report, ws_rel,
             "CutoverReconciliationError" in ws_text,
             "commit_workspace must reconcile crashed prior attempts "
             "(CutoverReconciliationError branches)")
    manifest_path = SRC / "contracts" / "manifest.py"
    manifest_text = (
        manifest_path.read_text(encoding="utf-8") if manifest_path.exists() else ""
    )
    _require(report, "src/discovery/contracts/manifest.py",
             "previous_generation_id" in manifest_text,
             "ActiveGenerationPointerV4 must define previous_generation_id")

    # ── Gate 4: resolve_active(verify_tree=True) performs a real content
    #    check against the manifest tree hash.
    _require(report, ws_rel,
             "if verify_tree:" in ws_text
             and "hash_workspace_tree" in ws_text
             and "manifest.workspace_tree_sha256" in ws_text,
             "resolve_active(verify_tree=True) must recompute hash_workspace_tree "
             "and compare it with manifest.workspace_tree_sha256")

    # ── Gate 6: retired migration drain tokens never reappear in
    #    src/scripts (reintroduction tombstone for the deleted v3->v4
    #    migration toolchain and its pending-candidate drain channel).
    for base in (SRC_ROOT, SCRIPTS):
        if not base.is_dir():
            continue
        for pyfile in sorted(base.rglob("*.py")):
            if pyfile.resolve() == verifier_self:
                continue
            fp = _file_label(pyfile)
            text = pyfile.read_text(encoding="utf-8")
            for token in ("legacy_candidate_seeds", "PendingCandidateStoreV4"):
                if token in text:
                    report.findings.append(Finding(
                        level="error", category="forbidden",
                        file=fp, line=0,
                        message=f"retired migration drain token '{token}' — the "
                                "one-time v3->v4 migration toolchain is deleted "
                                "and must not be reintroduced",
                    ))


class _ProductionLegacyVisitor(ast.NodeVisitor):
    """Gate 12/16 AST visitor for src/discovery/** production files."""

    def __init__(self, filepath: str, report: VerifierReport):
        self.filepath = filepath
        self.report = report
        self.is_coordinator = filepath.endswith("src/discovery/coordinator.py")

    def _error(self, category: str, line: int, message: str) -> None:
        self.report.findings.append(Finding(
            level="error", category=category,
            file=self.filepath, line=line, message=message,
        ))

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        # A bare `except Exception:` (no `as` binding) swallows failures
        # without any record; `except Exception as exc:` with logging is the
        # only permitted form in the coordinator.
        if (
            self.is_coordinator
            and isinstance(node.type, ast.Name)
            and node.type.id == "Exception"
            and node.name is None
        ):
            self._error(
                "forbidden", node.lineno,
                "coordinator must not swallow `except Exception:` without a "
                "binding — use `except Exception as exc:` and record it",
            )
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        # Journal/notebook attribution in the coordinator must never carry a
        # schema "3.0" whitelist; v4 is the only accepted production schema.
        if (
            self.is_coordinator
            and isinstance(node.value, str)
            and node.value == "3.0"
        ):
            self._error(
                "forbidden", node.lineno,
                "coordinator must not reference schema '3.0' — no legacy "
                "schema whitelist in production attribution",
            )
        self.generic_visit(node)


def _check_v4_migration_final_rules(report: VerifierReport) -> None:
    """Final-state gates (12, 15, 15b, 15c, 16) for Discovery v4.

    These pin the post-migration invariants that outlive the deleted
    one-time migration toolchain: production carries zero legacy
    symbols/parsers, both production discovery writers check the shared
    maintenance gate unconditionally, nothing imports the retired
    ``src.migrations`` package, production tools resolve the active v4
    workspace instead of retired flat constants, and no production parser
    accepts legacy notebook schemas.
    """
    # ── Gate 12: production carries zero legacy symbols / compat shells.
    legacy_tokens = (
        "legacy_unbound_profile",
        "is_legacy_unbound_profile",
        "LEGACY_UNBOUND",
    )
    batch_runtime_path = SRC / "runtime" / "batch_runtime.py"
    if SRC.is_dir():
        for pyfile in sorted(SRC.rglob("*.py")):
            if pyfile.name.startswith("__"):
                continue
            fp = _file_label(pyfile)
            text = pyfile.read_text(encoding="utf-8")
            for token in legacy_tokens:
                if token in text:
                    report.findings.append(Finding(
                        level="error", category="forbidden",
                        file=fp, line=0,
                        message=f"retired legacy symbol {token!r} in production "
                                "source — legacy unbound-profile compat is deleted",
                    ))
            if pyfile == batch_runtime_path:
                if "backward compat" in text:
                    report.findings.append(Finding(
                        level="error", category="forbidden",
                        file=fp, line=0,
                        message="batch_runtime must not carry 'backward compat' "
                                "comments — v4 single stack has no compat layer",
                    ))
                if "PageJournalStoreV4 as PageJournalStore" in text:
                    report.findings.append(Finding(
                        level="error", category="forbidden",
                        file=fp, line=0,
                        message="batch_runtime must not re-export "
                                "PageJournalStoreV4 as PageJournalStore",
                    ))
            tree = _scan_file(pyfile, report, fp)
            if tree is not None:
                _ProductionLegacyVisitor(fp, report).visit(tree)
    else:
        _require(report, "src/discovery", False,
                 "src/discovery is missing")

    # ── Gate 15: both production discovery writers check the maintenance
    #    gate unconditionally at startup (no --workspace-root bypass).
    for writer in ("discover_papers.py", "discover_papers_concurrent.py"):
        writer_path = SCRIPTS / writer
        writer_text = (
            writer_path.read_text(encoding="utf-8") if writer_path.exists() else ""
        )
        _require(report, f"scripts/{writer}",
                 writer_path.exists()
                 and "assert_discovery_write_allowed(" in writer_text,
                 f"{writer} must refuse to start while the discovery "
                 "maintenance lock is held (assert_discovery_write_allowed)")
        _require(report, f"scripts/{writer}",
                 "if not args.workspace_root" not in writer_text,
                 f"{writer} must not exempt --workspace-root from the "
                 "maintenance gate")

    # ── Gate 15b: nothing imports the retired one-time migration package
    #    (src.migrations is deleted; this guard scans importers and does not
    #    require the package to exist), and the maintenance gate lives in
    #    shared discovery infrastructure.
    maintenance_gate = SRC / "maintenance_gate.py"
    _require(report, "src/discovery/maintenance_gate.py",
             maintenance_gate.exists()
             and "assert_discovery_write_allowed" in maintenance_gate.read_text(
                 encoding="utf-8"),
             "src/discovery/maintenance_gate.py must host the shared "
             "maintenance gate")
    import re as _re
    _migration_import = _re.compile(
        r"^\s*(?:from|import)\s+src\.migrations", _re.MULTILINE
    )
    verifier_self = (SCRIPTS / "verify_discovery_final_architecture.py").resolve()
    for base in (SRC_ROOT, SCRIPTS):
        if not base.is_dir():
            continue
        for py in sorted(base.rglob("*.py")):
            if py.resolve() == verifier_self:
                continue
            rel = _file_label(py)
            _require(report, rel,
                     not _migration_import.search(py.read_text(encoding="utf-8")),
                     f"{rel} must not import the retired one-time migration "
                     "package (src.migrations)")

    # ── Gate 15c: production notebook tools resolve the active workspace;
    #    legacy flat path constants are migration/audit-only.
    flat_tokens = (
        "DISCOVERY_KEYWORD_NOTEBOOK_DIR",
        "DISCOVERY_PENDING_PAGES_DIR",
    )
    production_tools = (
        "manage_discovery_keywords.py",
        "configure_relevance_profiles.py",
        "sync_catalog_categories.py",
        "doctor_catalog_folders.py",
        "validate_v2_library.py",
    )
    for tool in production_tools:
        tool_path = SCRIPTS / tool
        if not tool_path.exists():
            continue
        tool_text = tool_path.read_text(encoding="utf-8")
        for token in flat_tokens:
            _require(report, f"scripts/{tool}",
                     token not in tool_text,
                     f"scripts/{tool} must resolve the active v4 workspace, "
                     f"not the retired flat constant {token}")
    catalog_reader = SRC_ROOT / "catalog_folders" / "reader.py"
    if catalog_reader.exists():
        reader_text = catalog_reader.read_text(encoding="utf-8")
        for token in flat_tokens:
            _require(report, "src/catalog_folders/reader.py",
                     token not in reader_text,
                     "create_safe_catalog_reader must use the active v4 "
                     f"workspace, not the retired flat constant {token}")

    # ── Gate 16: production has no parser that ACCEPTS schema 1.0/2.0/3.0.
    #
    # Judgement logic: the only legitimate mention of a legacy schema string
    # in src/discovery is the fail-closed rejection branch in
    # contracts/notebook.py: ``if version in ("1.0", "2.0", "3.0"): raise
    # UnsupportedNotebookSchemaError``.  Current-version constants (page
    # journal pagination "2.0", receipt "1.0", relevance profile "1.0") are
    # the *active* versions of their own artifact families and are not
    # legacy acceptance, so this gate keys on "3.0" only:
    #   - any "3.0" string occurrence outside the exact rejection tuple is
    #     a violation (this catches `== "3.0"` accept branches, wider tuples
    #     such as ("1.0", "2.0", "3.0", "4.0"), and whitelist literals);
    #   - the rejection tuple itself is only valid when the enclosing branch
    #     immediately raises UnsupportedNotebookSchemaError.
    rejection_re = re.compile(
        r"""in\s*\(\s*["']1\.0["']\s*,\s*["']2\.0["']\s*,\s*["']3\.0["']\s*\)"""
    )
    if SRC.is_dir():
        for pyfile in sorted(SRC.rglob("*.py")):
            if pyfile.name.startswith("__"):
                continue
            fp = _file_label(pyfile)
            lines = pyfile.read_text(encoding="utf-8").splitlines()
            for lineno, line in enumerate(lines, start=1):
                if "3.0" not in line:
                    continue
                if rejection_re.search(line):
                    window = "\n".join(lines[lineno - 1: lineno + 4])
                    if "raise UnsupportedNotebookSchemaError" not in window:
                        report.findings.append(Finding(
                            level="error", category="forbidden",
                            file=fp, line=lineno,
                            message="legacy schema tuple without an immediate "
                                    "raise UnsupportedNotebookSchemaError — "
                                    "old schemas must be rejected, never parsed",
                        ))
                    continue
                if re.search(r"""["']3\.0["']""", line):
                    report.findings.append(Finding(
                        level="error", category="forbidden",
                        file=fp, line=lineno,
                        message="production must not reference schema '3.0' "
                                "outside the fail-closed rejection branch in "
                                "contracts/notebook.py",
                    ))


def _check_http_and_flat_path_rules(report: VerifierReport) -> None:
    """Unified-HTTP and retired-flat-path gates.

    Gate H1: no module under ``src/discovery/`` may import ``requests``,
    ``httpx``, or ``urllib.request`` — all provider HTTP goes through
    ``providers/provider_client.py`` (the single allowed call-site).

    Gate H2: the retired flat discovery directory constants
    (``DISCOVERY_KEYWORD_NOTEBOOK_DIR`` / ``DISCOVERY_PENDING_PAGES_DIR``)
    may not be referenced from ``src/discovery/`` or the strict-v4 audit
    script; only the migration package and the legacy-recovery tool read
    them.  Production tools resolve directories through the active v4
    workspace (``resolve_active_runtime``).
    """
    allowed_http = (SRC / "providers" / "provider_client.py").resolve()
    for pyfile in sorted(SRC.rglob("*.py")):
        if pyfile.resolve() == allowed_http:
            continue
        fp = _file_label(pyfile)
        try:
            tree = ast.parse(pyfile.read_text(encoding="utf-8"))
        except SyntaxError:
            continue  # syntax failures are reported by the main scan
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in {"requests", "httpx"} or alias.name == "urllib.request":
                        report.findings.append(Finding(
                            level="error", category="forbidden",
                            file=fp, line=node.lineno,
                            message=f"direct HTTP import {alias.name!r} — all provider "
                                    "HTTP goes through providers/provider_client.py",
                        ))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                top = module.split(".")[0]
                from_urllib_request = module == "urllib.request" or (
                    module == "urllib"
                    and any(alias.name == "request" for alias in node.names)
                )
                if top in {"requests", "httpx"} or from_urllib_request:
                    report.findings.append(Finding(
                        level="error", category="forbidden",
                        file=fp, line=node.lineno,
                        message=f"direct HTTP import from {module!r} — all provider "
                                "HTTP goes through providers/provider_client.py",
                    ))

    flat_constants = ("DISCOVERY_KEYWORD_NOTEBOOK_DIR", "DISCOVERY_PENDING_PAGES_DIR")
    audit_script = SCRIPTS / "audit_discovery_keyword_index_sources.py"
    targets = list(sorted(SRC.rglob("*.py")))
    if audit_script.exists():
        targets.append(audit_script)
    for pyfile in targets:
        fp = _file_label(pyfile)
        text = pyfile.read_text(encoding="utf-8")
        for name in flat_constants:
            if name in text:
                report.findings.append(Finding(
                    level="error", category="forbidden", file=fp, line=0,
                    message=f"reference to retired flat discovery path constant {name} — "
                            "resolve directories through the active v4 workspace "
                            "(resolve_active_runtime)",
                ))


def verify_discovery_final_architecture() -> VerifierReport:
    """Run all checks and return a VerifierReport."""
    report = VerifierReport()

    # Scan all discovery source files
    for pyfile in sorted(SRC.rglob("*.py")):
        verify_file(pyfile, report)

    # Phase 7 post-scan rules
    _check_phase7_rules(report)

    # v4 single-stack post-scan rules
    _check_single_stack_rules(report)

    # Post-migration hardening gates (workspace cutover + tombstones)
    _check_migration_hardening_rules(report)

    # Post-migration final-state gates (12, 15, 15b, 15c, 16)
    _check_v4_migration_final_rules(report)

    # Unified-HTTP + retired flat-path gates
    _check_http_and_flat_path_rules(report)

    return report


def main() -> int:
    report = verify_discovery_final_architecture()

    if "--json" in sys.argv:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        if report.passed:
            print("[OK] discovery final architecture verified")
        else:
            print(f"[FAIL] {len(report.errors)} architecture violation(s):")
            for f in report.errors:
                print(f"  {f.file}:{f.line}: [{f.category}] {f.message}")
        if report.warnings:
            for w in report.warnings:
                print(f"  {w.file}:{w.line}: [WARN] {w.message}")

    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
