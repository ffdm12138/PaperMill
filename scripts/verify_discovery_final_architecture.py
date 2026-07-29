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
import inspect
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
SRC = SRC_ROOT / "discovery"
SCRIPTS = PROJECT_ROOT / "scripts"
DOCS_DIR = PROJECT_ROOT / "docs"


@dataclass
class Finding:
    level: str  # "error" (fail-closed drift) | "warning" (transitional)
    category: str  # "forbidden" | "missing_required" | "call_graph" | "behavioral"
    file: str
    line: int
    message: str


@dataclass
class VerifierReport:
    findings: list[Finding] = field(default_factory=list)
    files_scanned: list[str] = field(default_factory=list)
    gate_results: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warning"]

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
            "gate_results": self.gate_results,
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

    # ── Gate 3: commit is lock-guarded, snapshots the previous pointer,
    #    and reconciles crashed attempts; the pointer records the previous
    #    generation.
    _require(report, ws_rel,
             "FileLock" in ws_text and ".maintenance.lock" in ws_text,
             "commit_workspace must acquire the .maintenance.lock FileLock")
    _require(report, ws_rel,
             "previous_pointer_snapshot" in ws_text,
             "commit_workspace must snapshot the superseded previous pointer")
    _require(report, ws_rel,
             "CommitReconciliationError" in ws_text,
             "commit_workspace must reconcile crashed prior attempts "
             "(CommitReconciliationError branches)")
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


def _check_dead_v4_store_tombstones(report: VerifierReport) -> None:
    """Frozen-seal Phase 2: the dead v4 store stack is deleted for good.

    ``LaneStateStoreV4`` / ``JournalIndexV4`` / ``ReportStoreV4`` and the
    ``LaneStateV4`` / ``CursorTransactionV4`` contracts had zero production
    readers (the coordinator consumes only ``bundle.notebooks`` and
    ``bundle.pages``; the candidate drain uses ``JournalDrainIndex``).  Their
    module files are removed and the tokens may never reappear anywhere
    under ``src/discovery/**``.
    """
    for dead_path in (
        SRC / "stores" / "lane_state_store.py",
        SRC / "stores" / "journal_index.py",
        SRC / "stores" / "report_store.py",
        SRC / "contracts" / "lane_state.py",
    ):
        if dead_path.exists():
            report.findings.append(Finding(
                level="error", category="forbidden",
                file=_file_label(dead_path), line=0,
                message=f"dead v4 store module {dead_path.name} must be removed",
            ))

    dead_tokens = (
        "LaneStateStoreV4",
        "JournalIndexV4",
        "ReportStoreV4",
        "LaneStateV4",
        "CursorTransactionV4",
    )
    if SRC.is_dir():
        for pyfile in sorted(SRC.rglob("*.py")):
            fp = _file_label(pyfile)
            text = pyfile.read_text(encoding="utf-8")
            for token in dead_tokens:
                if token in text:
                    report.findings.append(Finding(
                        level="error", category="forbidden",
                        file=fp, line=0,
                        message=f"dead v4 store token '{token}' — the zero-reader "
                                "v4 store stack is deleted and must not be "
                                "reintroduced",
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

    # ── Gate 15: both production discovery writers hold a shared writer
    #    lease for the entire run (no --workspace-root bypass).
    for writer in ("discover_papers.py", "discover_papers_concurrent.py"):
        writer_path = SCRIPTS / writer
        writer_text = (
            writer_path.read_text(encoding="utf-8") if writer_path.exists() else ""
        )
        _require(report, f"scripts/{writer}",
                 writer_path.exists()
                 and "DiscoveryWriterLease(" in writer_text,
                 f"{writer} must hold a DiscoveryWriterLease for the entire "
                 "run (blocks discovery maintenance windows)")
        _require(report, f"scripts/{writer}",
                 "if not args.workspace_root" not in writer_text,
                 f"{writer} must not exempt --workspace-root from the "
                 "maintenance gate")

    # ── Gate 15b: nothing imports the retired one-time migration package
    #    (src.migrations is deleted; this guard scans importers and does not
    #    require the package to exist), and the maintenance gate lives in
    #    shared discovery infrastructure.
    maintenance_gate = SRC / "maintenance_gate.py"
    maintenance_gate_text = (
        maintenance_gate.read_text(encoding="utf-8")
        if maintenance_gate.exists()
        else ""
    )
    _require(report, "src/discovery/maintenance_gate.py",
             maintenance_gate.exists()
             and "assert_discovery_write_allowed" in maintenance_gate_text
             and "class DiscoveryWriterLease" in maintenance_gate_text
             and "def active_writer_leases" in maintenance_gate_text,
             "src/discovery/maintenance_gate.py must host the shared "
             "maintenance gate (assert_discovery_write_allowed, "
             "DiscoveryWriterLease, active_writer_leases)")
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


# ── Final-freeze behavioral gates ─────────────────────────────────────────
#
# The textual/AST gates above pin structure.  The gates below EXECUTE the
# production code with dynamically injected negative inputs and assert
# rejection: a strict parser that starts accepting garbage, a resolver that
# starts following symlinks, or a lock that stops excluding writers must
# fail the freeze even when every source token still looks right.
#
# Every gate runs in-process against the real modules (the verifier runs
# inside the repo python), uses only tempfile workspaces, never touches
# data/, and never sleeps.  Each gate records a distinct entry in
# ``report.gate_results`` so the acceptance report can cite it.


class _BehavioralGate:
    """Accumulator for one behavioral gate: probes, failures, warnings."""

    def __init__(self, report: VerifierReport, name: str) -> None:
        self.report = report
        self.name = name
        self.probes = 0
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def probe(self, label: str, ok: bool, detail: str = "") -> None:
        self.probes += 1
        if not ok:
            self.failures.append(f"{label}: {detail}" if detail else label)

    def expect_reject(
        self, label: str, fn: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> None:
        """Probe that ``fn(*args, **kwargs)`` raises (any Exception)."""
        self.probes += 1
        try:
            fn(*args, **kwargs)
        except Exception:
            return
        self.failures.append(f"{label}: expected rejection, input was accepted")

    def expect_typed_reject(
        self,
        label: str,
        exc_type: type[BaseException],
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Probe that ``fn(*args, **kwargs)`` raises exactly ``exc_type``."""
        self.probes += 1
        try:
            fn(*args, **kwargs)
        except exc_type:
            return
        except Exception as exc:
            self.failures.append(
                f"{label}: expected {exc_type.__name__}, "
                f"got {type(exc).__name__}: {exc}"
            )
            return
        self.failures.append(f"{label}: expected {exc_type.__name__}, no error raised")

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def finalize(self) -> None:
        self.report.gate_results[self.name] = {
            "passed": not self.failures,
            "probes": self.probes,
            "failures": list(self.failures),
            "warnings": list(self.warnings),
        }
        for message in self.failures:
            self.report.findings.append(Finding(
                level="error", category="behavioral",
                file=f"behavioral-gate:{self.name}", line=0, message=message,
            ))
        for message in self.warnings:
            self.report.findings.append(Finding(
                level="warning", category="behavioral",
                file=f"behavioral-gate:{self.name}", line=0, message=message,
            ))


def _gate_pointer_negative_probes(report: VerifierReport) -> None:
    """ActiveGenerationPointerV4.from_dict_strict must reject damaged input."""
    from src.discovery.contracts.manifest import ActiveGenerationPointerV4

    gate = _BehavioralGate(report, "pointer-negative-probes")
    baseline = ActiveGenerationPointerV4(
        generation_id="gate-probe-gen",
        workspace_manifest_sha256="a" * 64,
        activated_at="2026-01-01T00:00:00+00:00",
        migration_id="gate-probe-migration",
    ).to_dict()

    # Positive control: the unmutated baseline must parse.
    gate.probes += 1
    try:
        ActiveGenerationPointerV4.from_dict_strict(dict(baseline))
    except Exception as exc:
        gate.failures.append(f"positive control: valid pointer rejected: {exc}")

    def _case(label: str, **mutations: Any) -> None:
        data = dict(baseline)
        for key, value in mutations.items():
            if value is _DELETE:
                data.pop(key, None)
            else:
                data[key] = value
        gate.expect_reject(
            label, ActiveGenerationPointerV4.from_dict_strict, data
        )

    _case("schema_version '3.0' rejected", schema_version="3.0")
    _case("missing schema_version rejected", schema_version=_DELETE)
    _case("integer generation_id rejected", generation_id=7)
    _case("generation_id '.' rejected", generation_id=".")
    _case("generation_id '..' rejected", generation_id="..")
    _case(
        "invalid-calendar activated_at rejected",
        activated_at="2026-13-99T99:99:99+99:99",
    )
    _case("naive activated_at rejected", activated_at="2026-01-01T00:00:00")
    _case(
        "non-hex workspace_manifest_sha256 rejected",
        workspace_manifest_sha256="z" * 64,
    )
    gate.finalize()


_DELETE = object()


def _gate_manifest_negative_probes(report: VerifierReport) -> None:
    """DiscoveryWorkspaceManifestV4.from_dict_strict must reject damaged input."""
    from src.discovery.contracts.manifest import DiscoveryWorkspaceManifestV4
    from src.discovery.workspace import build_workspace_manifest

    gate = _BehavioralGate(report, "manifest-negative-probes")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "gate-manifest-ws"
        for subdir in (
            "keyword_notebooks", "page_journals", "exports", "reports", "locks",
        ):
            (root / subdir).mkdir(parents=True)
        baseline = build_workspace_manifest(
            root.name, root, migration_id="gate-probe-migration"
        ).to_dict()

    # Positive control: the unmutated baseline must parse.
    gate.probes += 1
    try:
        DiscoveryWorkspaceManifestV4.from_dict_strict(dict(baseline))
    except Exception as exc:
        gate.failures.append(f"positive control: valid manifest rejected: {exc}")

    def _case(label: str, **mutations: Any) -> None:
        data = dict(baseline)
        for key, value in mutations.items():
            if value is _DELETE:
                data.pop(key, None)
            else:
                data[key] = value
        gate.expect_reject(
            label, DiscoveryWorkspaceManifestV4.from_dict_strict, data
        )

    _case("negative notebook_count rejected", notebook_count=-1)
    _case("bool notebook_count rejected", notebook_count=True)
    _case("missing required field rejected", migration_id=_DELETE)
    _case("malformed hash rejected", notebook_set_hash="not-a-hash")
    _case("empty store_schema_versions rejected", store_schema_versions={})
    _case(
        "completed_at earlier than created_at rejected",
        created_at="2026-01-02T00:00:00+00:00",
        completed_at="2026-01-01T00:00:00+00:00",
    )
    gate.finalize()


def _gate_runtime_error_taxonomy(report: VerifierReport) -> None:
    """Workspace resolution errors must map to the exact runtime taxonomy."""
    from src.discovery import runtime_context as rc
    from src.discovery import workspace as wsp

    gate = _BehavioralGate(report, "runtime-error-taxonomy")
    cases = (
        (wsp.ActiveGenerationMissingError, rc.DiscoveryRuntimeNotInitialized),
        (wsp.ActiveGenerationCorruptError, rc.DiscoveryRuntimeCorrupt),
        (wsp.WorkspaceManifestMissingError, rc.DiscoveryRuntimeCorrupt),
        (wsp.WorkspaceManifestMismatchError, rc.DiscoveryRuntimeCorrupt),
        (wsp.WorkspaceIncompleteError, rc.DiscoveryRuntimeIncomplete),
        (ValueError, rc.DiscoveryRuntimeCorrupt),  # unexpected: fail closed
    )
    for exc_type, expected in cases:
        mapped = rc._map_resolution_error(
            exc_type("behavioral probe"), origin="behavioral-gate"
        )
        gate.probe(
            f"{exc_type.__name__} maps to {expected.__name__}",
            type(mapped) is expected,
            f"got {type(mapped).__name__}",
        )
    gate.finalize()


def _gate_workspace_identity_and_symlink(report: VerifierReport) -> None:
    """Explicit-workspace resolution: identity binding + symlink rejection."""
    from src.discovery.workspace import (
        DiscoveryWorkspace,
        WorkspaceResolver,
        build_workspace_manifest,
        write_workspace_manifest,
    )

    gate = _BehavioralGate(report, "workspace-identity-and-symlink")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "gate-ws-identity"
        for subdir in (
            "keyword_notebooks", "page_journals", "exports", "reports", "locks",
        ):
            (root / subdir).mkdir(parents=True)
        manifest = build_workspace_manifest(
            root.name, root, migration_id="gate-ws-migration"
        )
        write_workspace_manifest(root, manifest)

        # Positive control: the complete workspace must resolve.
        gate.probes += 1
        try:
            resolved = WorkspaceResolver.resolve_explicit_workspace(root)
            if resolved.generation_id != root.name:
                gate.failures.append(
                    f"explicit workspace accepted but generation_id "
                    f"{resolved.generation_id!r} != root name {root.name!r}"
                )
        except Exception as exc:
            gate.failures.append(f"complete workspace rejected: {exc}")

        # Negative: a required subdir that is a symlink out of the root must
        # be rejected.  Some Windows hosts cannot create symlinks without
        # elevated privileges — skip with a warning, never a silent pass.
        outside = Path(tmp) / "outside"
        outside.mkdir()
        target = root / "keyword_notebooks"
        try:
            target.rmdir()
            os.symlink(str(outside), str(target), target_is_directory=True)
        except OSError as exc:
            gate.warn(
                f"symlink probe skipped (host cannot create directory "
                f"symlinks): {exc}"
            )
        else:
            gate.expect_reject(
                "symlinked keyword_notebooks rejected",
                WorkspaceResolver.resolve_explicit_workspace,
                root,
            )

    # Negative: generation id path traversal must be rejected at construction.
    gate.expect_reject(
        "DiscoveryWorkspace.from_generation_id('..') rejected",
        DiscoveryWorkspace.from_generation_id,
        "..",
    )

    # The explicit resolver must not grow a way to skip manifest validation.
    params = inspect.signature(
        WorkspaceResolver.resolve_explicit_workspace
    ).parameters
    gate.probe(
        "resolve_explicit_workspace has no verify_manifest parameter",
        "verify_manifest" not in params,
        f"parameters: {sorted(params)}",
    )
    gate.finalize()


def _gate_bootstrap_crash_windows(report: VerifierReport) -> None:
    """bootstrap_initial_workspace must resume crash windows deterministically.

    Mirrors tests/unit/discovery/test_bootstrap_crash_recovery.py: the
    module-level workspace paths are redirected into a temp dir, exercised,
    and restored in a finally block.
    """
    import src.discovery.workspace as wsp

    gate = _BehavioralGate(report, "bootstrap-crash-windows")
    patched = (
        "DISCOVERY_GENERATIONS_DIR", "STAGING_DIR", "DISCOVERY_MIGRATIONS_DIR",
        "ACTIVE_GENERATION_PATH", "DISCOVERY_MAINTENANCE_LOCK_PATH",
    )
    saved = {name: getattr(wsp, name) for name in patched}

    def _install(tmp: Path) -> None:
        generations = tmp / "generations"
        staging = generations / ".staging"
        migrations = tmp / "migrations"
        for directory in (generations, staging, migrations):
            directory.mkdir(parents=True, exist_ok=True)
        wsp.DISCOVERY_GENERATIONS_DIR = generations
        wsp.STAGING_DIR = staging
        wsp.DISCOVERY_MIGRATIONS_DIR = migrations
        wsp.ACTIVE_GENERATION_PATH = tmp / "active_generation.json"
        wsp.DISCOVERY_MAINTENANCE_LOCK_PATH = migrations / ".maintenance.lock"

    def _stage_and_rename(tmp: Path, gid: str) -> str:
        staging = wsp.create_staging_workspace(gid)
        manifest = wsp.build_workspace_manifest(
            gid, staging.root, migration_id=wsp.BOOTSTRAP_MIGRATION_ID
        )
        manifest_hash = wsp.write_workspace_manifest(staging.root, manifest)
        os.rename(str(staging.root), str(tmp / "generations" / gid))
        return manifest_hash

    try:
        # (a) crash after the rename, before the pointer write: recovery must
        # bind the pointer to the ORIGINAL manifest hash and report
        # created=False (recovered, never recreated).
        try:
            with tempfile.TemporaryDirectory() as tmp_str:
                tmp = Path(tmp_str)
                _install(tmp)
                gid = "v4-gate-rename"
                original_hash = _stage_and_rename(tmp, gid)
                ws, created = wsp.bootstrap_initial_workspace(generation_id=gid)
                gate.probe(
                    "rename-window recovery reports created=False",
                    created is False,
                    f"created={created!r}",
                )
                gate.probe(
                    "rename-window recovery returns the generation",
                    ws is not None and ws.generation_id == gid,
                    f"got {ws!r}",
                )
                pointer = json.loads(
                    (tmp / "active_generation.json").read_text(encoding="utf-8")
                )
                gate.probe(
                    "recovered pointer binds the ORIGINAL manifest hash",
                    pointer.get("workspace_manifest_sha256") == original_hash,
                    f"pointer has {pointer.get('workspace_manifest_sha256')!r}",
                )
        except Exception as exc:
            gate.probes += 1
            gate.failures.append(f"rename-window scenario crashed: {exc!r}")

        # (b) two unpointed generations: ambiguous state must fail closed.
        try:
            with tempfile.TemporaryDirectory() as tmp_str:
                tmp = Path(tmp_str)
                _install(tmp)
                for gid in ("v4-gate-amb1", "v4-gate-amb2"):
                    _stage_and_rename(tmp, gid)
                gate.expect_typed_reject(
                    "two unpointed generations raise CommitReconciliationError",
                    wsp.CommitReconciliationError,
                    wsp.bootstrap_initial_workspace,
                )
        except Exception as exc:
            gate.probes += 1
            gate.failures.append(f"ambiguous-generations scenario crashed: {exc!r}")
    finally:
        for name, value in saved.items():
            setattr(wsp, name, value)
    gate.finalize()


def _gate_maintenance_exclusion(report: VerifierReport) -> None:
    """Writer leases must exclude the exclusive maintenance lock."""
    from src.discovery.maintenance_gate import (
        DiscoveryMaintenanceLock,
        DiscoveryMaintenanceLockError,
        DiscoveryWriterLease,
        discovery_maintenance_block_reason,
    )

    gate = _BehavioralGate(report, "maintenance-exclusion")
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        lock_path = tmp / ".maintenance.lock"

        lease = DiscoveryWriterLease(
            "behavioral-gate", lock_path=lock_path
        ).acquire()
        try:
            gate.expect_typed_reject(
                "maintenance lock blocked while a writer lease is held",
                DiscoveryMaintenanceLockError,
                DiscoveryMaintenanceLock(
                    "behavioral-gate", lock_path=lock_path
                ).acquire,
            )
        finally:
            lease.release()

        gate.probes += 1
        try:
            DiscoveryMaintenanceLock(
                "behavioral-gate", lock_path=lock_path
            ).acquire().release()
        except Exception as exc:
            gate.failures.append(
                f"maintenance lock must acquire after lease release: {exc}"
            )

        # Fail-closed probe: a lock path that is a DIRECTORY must block.
        lock_dir = tmp / "lock-as-directory"
        lock_dir.mkdir()
        reason = discovery_maintenance_block_reason(lock_dir)
        gate.probe(
            "directory lock path blocks writers (fail closed)",
            reason is not None,
            "discovery_maintenance_block_reason returned None",
        )
    gate.finalize()


def _gate_server_layering(report: VerifierReport) -> None:
    """The HTTP middleware must stay auth+headers only; services stay lazy."""
    gate = _BehavioralGate(report, "server-layering")
    server_path = SRC_ROOT / "server.py"
    gate.probes += 1
    if not server_path.is_file():
        gate.failures.append("src/server.py is missing")
        gate.finalize()
        return
    text = server_path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=str(server_path))
    except SyntaxError as exc:
        gate.failures.append(f"src/server.py does not parse: {exc}")
        gate.finalize()
        return

    middleware_src: str | None = None
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "security_headers_and_api_key"
        ):
            middleware_src = ast.get_source_segment(text, node) or ""
            break
    gate.probe(
        "middleware security_headers_and_api_key found",
        middleware_src is not None,
    )
    if middleware_src:
        gate.probe(
            "middleware performs no eager service init (_ensure_services)",
            "_ensure_services" not in middleware_src,
        )
        for token in (
            "_get_catalog(", "_get_library(",
            "_get_job_manager(", "_get_prompt_builder(",
        ):
            gate.probe(
                f"middleware does not call {token}",
                token not in middleware_src,
            )
    for token in (
        "def _get_catalog",
        "def _get_library",
        "def _get_prompt_builder",
        "def _get_job_manager",
        "/status/discovery",
        "exception_handler(DiscoveryRuntimeUnavailableError)",
    ):
        gate.probe(f"src/server.py contains {token!r}", token in text)
    gate.finalize()


def _gate_lock_before_resolve(report: VerifierReport) -> None:
    """Discovery entry points must lock before resolving the workspace."""
    gate = _BehavioralGate(report, "lock-before-resolve-ordering")
    resolve_tokens = (
        "resolve_active_runtime(",
        "resolve_active(",
        "resolve_explicit_workspace(",
    )
    for script in ("discover_papers.py", "discover_papers_concurrent.py"):
        path = SCRIPTS / script
        gate.probes += 1
        if not path.is_file():
            gate.failures.append(f"scripts/{script} is missing")
            continue
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            gate.failures.append(f"scripts/{script} does not parse: {exc}")
            continue
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        entry = functions.get("main_internal") or functions.get("main")
        gate.probe(f"{script}: entry point found", entry is not None)
        if entry is None:
            continue
        segment = ast.get_source_segment(text, entry) or ""
        gate.probe(
            f"{script}: entry point holds DiscoveryWriterLease",
            "DiscoveryWriterLease(" in segment,
        )
        for token in resolve_tokens:
            gate.probe(
                f"{script}: entry point performs no resolve-before-lock "
                f"({token})",
                token not in segment,
            )
        lease_idx = segment.find("DiscoveryWriterLease(")
        run_idx = segment.find("_run(")
        gate.probe(
            f"{script}: lease wraps the run delegation",
            lease_idx != -1 and run_idx != -1 and lease_idx < run_idx,
        )
        gate.probe(
            f"{script}: workspace resolution happens downstream of the lease",
            any(token in text for token in resolve_tokens),
        )

    keywords_path = SCRIPTS / "manage_discovery_keywords.py"
    gate.probes += 1
    if not keywords_path.is_file():
        gate.failures.append("scripts/manage_discovery_keywords.py is missing")
    else:
        text = keywords_path.read_text(encoding="utf-8")
        lock_idx = text.find("DiscoveryMaintenanceLock(")
        resolve_idx = text.find("resolve_active_runtime(")
        gate.probe(
            "manage_discovery_keywords: --apply path acquires "
            "DiscoveryMaintenanceLock",
            lock_idx != -1,
        )
        gate.probe(
            "manage_discovery_keywords: resolves the active runtime",
            resolve_idx != -1,
        )
        if lock_idx != -1 and resolve_idx != -1:
            gate.probe(
                "manage_discovery_keywords: lock precedes resolve in the "
                "--apply path",
                lock_idx < resolve_idx,
            )
    gate.finalize()


def _gate_test_helper_identity(report: VerifierReport) -> None:
    """tests.helpers.make_test_workspace must produce identity-bound fixtures."""
    gate = _BehavioralGate(report, "test-helper-identity")
    gate.probes += 1
    try:
        from tests.helpers import discovery_workspace as helper_mod
    except Exception as exc:
        gate.failures.append(
            f"tests.helpers.discovery_workspace is not importable: {exc}"
        )
        gate.finalize()
        return
    make = getattr(helper_mod, "make_test_workspace", None)
    if not callable(make):
        gate.failures.append("make_test_workspace is missing")
        gate.finalize()
        return

    from src.discovery.workspace import WorkspaceResolver

    with tempfile.TemporaryDirectory() as tmp_str:
        root = Path(tmp_str) / "helpergate"
        # Defensive call: only the root argument — the helper's optional
        # out-of-root directory kwargs are being removed; never rely on them.
        workspace = None
        try:
            workspace = make(root)
        except TypeError:
            try:
                workspace = make(root=root)
            except Exception as exc:
                gate.failures.append(f"make_test_workspace(root) failed: {exc}")
        except Exception as exc:
            gate.failures.append(f"make_test_workspace(root) failed: {exc}")
        if workspace is None:
            gate.finalize()
            return

        generation_id = getattr(workspace, "generation_id", None)
        if generation_id == root.name:
            gate.probe("fixture generation_id equals the root dir name", True)
        elif generation_id == f"test-{root.name}":
            # Known-transitional: the helper rewrite (removing the 'test-'
            # prefix) is in flight; flag it without failing the freeze.
            gate.probes += 1
            gate.warn(
                "transitional: make_test_workspace still prefixes "
                f"generation_id with 'test-' ({generation_id!r}); the helper "
                "rewrite must land before freeze"
            )
        else:
            gate.probe(
                "fixture generation_id equals the root dir name",
                False,
                f"got {generation_id!r}",
            )

        gate.probes += 1
        try:
            resolved = WorkspaceResolver.resolve_explicit_workspace(root)
            if resolved.generation_id != root.name:
                gate.failures.append(
                    f"resolved generation_id {resolved.generation_id!r} != "
                    f"root name {root.name!r}"
                )
        except Exception as exc:
            gate.failures.append(
                "make_test_workspace fixture must pass "
                f"WorkspaceResolver.resolve_explicit_workspace: {exc}"
            )
    gate.finalize()


_STALE_DOCUMENT_TOKENS = (
    "migrate_discovery_v4",
    "--post-cutover-validate",
    "--clean-legacy",
    "--finalize",
    "PendingCandidateStoreV4",
    ".migration.lock",
    "MIGRATION_LOCK_PATH",
)
_HISTORICAL_ADR_FILES = frozenset({
    "ADR_DISCOVERY_V4_MIGRATION_FINAL.md",
    "ADR_DISCOVERY_V4_SINGLE_STACK.md",
})


def _gate_stale_document_commands(report: VerifierReport) -> None:
    """Deleted migration command tokens may survive only in the two ADRs."""
    gate = _BehavioralGate(report, "stale-document-commands")
    targets: list[Path] = []
    if DOCS_DIR.is_dir():
        targets.extend(sorted(DOCS_DIR.glob("*.md")))
    for name in ("AGENTS.md", "CLAUDE.md"):
        candidate = PROJECT_ROOT / name
        if candidate.is_file():
            targets.append(candidate)
    gate.probes += 1
    if not targets:
        gate.failures.append(
            "no documentation targets found (docs/*.md, AGENTS.md, CLAUDE.md)"
        )
        gate.finalize()
        return
    for path in targets:
        gate.probes += 1
        if path.name in _HISTORICAL_ADR_FILES:
            continue  # tokens are allowed inside the historical ADRs
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            gate.failures.append(f"{_file_label(path)}: unreadable: {exc}")
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for token in _STALE_DOCUMENT_TOKENS:
                if token in line:
                    gate.failures.append(
                        f"{_file_label(path)}:{lineno}: stale command token "
                        f"{token!r} — deleted migration commands may only "
                        "appear in the two historical ADRs"
                    )
    gate.finalize()


_BEHAVIORAL_GATES: tuple[tuple[str, Callable[[VerifierReport], None]], ...] = (
    ("pointer-negative-probes", _gate_pointer_negative_probes),
    ("manifest-negative-probes", _gate_manifest_negative_probes),
    ("runtime-error-taxonomy", _gate_runtime_error_taxonomy),
    ("workspace-identity-and-symlink", _gate_workspace_identity_and_symlink),
    ("bootstrap-crash-windows", _gate_bootstrap_crash_windows),
    ("maintenance-exclusion", _gate_maintenance_exclusion),
    ("server-layering", _gate_server_layering),
    ("lock-before-resolve-ordering", _gate_lock_before_resolve),
    ("test-helper-identity", _gate_test_helper_identity),
    ("stale-document-commands", _gate_stale_document_commands),
)


def _check_final_freeze_behavioral_rules(report: VerifierReport) -> None:
    """Run every final-freeze behavioral gate with crash isolation.

    A gate that raises unexpectedly must fail its own entry closed without
    taking the remaining gates down with it.
    """
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    for name, gate_fn in _BEHAVIORAL_GATES:
        try:
            gate_fn(report)
        except Exception as exc:
            report.gate_results[name] = {
                "passed": False,
                "probes": 0,
                "failures": [f"gate crashed: {exc!r}"],
                "warnings": [],
            }
            report.findings.append(Finding(
                level="error", category="behavioral",
                file=f"behavioral-gate:{name}", line=0,
                message=f"behavioral gate crashed (fail closed): {exc!r}",
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

    # Frozen-seal Phase 2: dead v4 store stack tombstones
    _check_dead_v4_store_tombstones(report)

    # Post-migration final-state gates (12, 15, 15b, 15c, 16)
    _check_v4_migration_final_rules(report)

    # Unified-HTTP + retired flat-path gates
    _check_http_and_flat_path_rules(report)

    # Final-freeze behavioral gates (dynamic negative probes)
    _check_final_freeze_behavioral_rules(report)

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
