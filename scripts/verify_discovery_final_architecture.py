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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src" / "discovery"
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


def _scan_file(path: Path) -> ast.AST | None:
    """Parse a Python file, returning None on syntax error."""
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
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

        # keyword_notebook must use GenerationHistoryEntry.from_dict_strict
        if "keyword_notebook" in fp:
            if "GenerationHistoryEntry.from_dict_strict" not in self._found_required:
                self._error(0, "keyword_notebook must use GenerationHistoryEntry.from_dict_strict for strict validation")

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

    tree = _scan_file(path)
    if tree is None:
        return

    filepath = str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
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
                file="src/discovery/report_builder.py", line=0,
                message="KeywordDiscoveryReport must have durable_progress: bool field",
            ))


def verify_discovery_final_architecture() -> VerifierReport:
    """Run all checks and return a VerifierReport."""
    report = VerifierReport()

    # Scan all discovery source files
    for pyfile in sorted(SRC.rglob("*.py")):
        verify_file(pyfile, report)

    # Phase 7 post-scan rules
    _check_phase7_rules(report)

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
