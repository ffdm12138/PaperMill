"""Unit tests for the parallel acceptance scheduler and marker split.

Covers the pure-logic pieces of the parallel fast gate without spawning
subprocesses:
- ``_run_groups_concurrently`` (injected fake ``run_one``)
- the full-gate marker split constants (parallel chunk + sequential residue
  must partition FULL_MARKERS exactly)
- ``_effective_jobs`` resolution
- ``scan_repo_bytecode`` pruned-walk parity
- ``child_env`` PYTHONIOENCODING guarantee
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from scripts.agent_acceptance import (
    FAST_MARKERS,
    FULL_MARKERS,
    FULL_PARALLEL_MARKERS,
    FULL_RESIDUE_MARKERS,
    _effective_jobs,
    _run_groups_concurrently,
)
from scripts.test_runtime_workspace import TestRuntimeWorkspace, scan_repo_bytecode

pytestmark = pytest.mark.unit


class TestMarkerSplit:
    def test_marker_expressions_are_frozen(self):
        """The split is load-bearing: parallel ∪ residue == FULL, disjoint.

        ``not process and not slow ...`` and ``(process or slow) and ...``
        partition ``not stress and not external`` by construction.  Freezing
        the exact strings here means any edit to the selection logic must
        consciously update this test.
        """
        assert FULL_MARKERS == "not stress and not external"
        assert FULL_PARALLEL_MARKERS == (
            "not process and not slow and not stress and not external"
        )
        assert FULL_RESIDUE_MARKERS == "(process or slow) and not stress and not external"
        assert FAST_MARKERS == FULL_PARALLEL_MARKERS

    @pytest.mark.parametrize(
        "process,slow,stress,external", [
            (p, s, t, e)
            for p in (False, True) for s in (False, True)
            for t in (False, True) for e in (False, True)
        ],
    )
    def test_split_partitions_full_selection(self, process, slow, stress, external):
        """Evaluate the three expressions against every marker combination."""
        names = {"process": process, "slow": slow, "stress": stress, "external": external}

        def evaluate(expression: str) -> bool:
            return bool(eval(expression, {"__builtins__": {}}, names))  # noqa: S307

        in_full = evaluate(FULL_MARKERS)
        in_parallel = evaluate(FULL_PARALLEL_MARKERS)
        in_residue = evaluate(FULL_RESIDUE_MARKERS)
        assert in_parallel + in_residue == (1 if in_full else 0)


class TestEffectiveJobs:
    def test_no_parallel_forces_one(self):
        assert _effective_jobs(8, no_parallel=True) == 1

    def test_explicit_value_respected(self):
        assert _effective_jobs(3, no_parallel=False) == 3

    def test_auto_is_bounded(self):
        jobs = _effective_jobs(0, no_parallel=False)
        assert 2 <= jobs <= 12

    def test_negative_rejected(self):
        with pytest.raises(SystemExit):
            _effective_jobs(-1, no_parallel=False)


class TestGroupScheduler:
    GROUPS = [(f"g{i}", [f"tests/g{i}"]) for i in range(5)]

    def test_all_groups_execute_and_order_is_declaration_order(self):
        executed: list[str] = []
        lock = threading.Lock()

        def run_one(name, paths, cancel):
            with lock:
                executed.append(name)
            # Reverse the finishing order: later groups finish first.
            time.sleep(0.01 * (5 - int(name[1:])))
            return 0, f"out-{name}\n"

        reported: list[str] = []
        results = _run_groups_concurrently(
            self.GROUPS, run_one, jobs=5,
            on_result=lambda name, rc, output: reported.append(name),
        )
        assert sorted(executed) == [g for g, _ in self.GROUPS]
        assert [name for name, _, _ in results] == [g for g, _ in self.GROUPS]
        assert reported == [g for g, _ in self.GROUPS]
        assert all(rc == 0 for _, rc, _ in results)

    def test_one_failure_does_not_cancel_others(self):
        def run_one(name, paths, cancel):
            if name == "g1":
                return 2, "boom\n"
            assert not cancel.is_set()
            return 0, "ok\n"

        results = _run_groups_concurrently(self.GROUPS, run_one, jobs=2)
        by_name = {name: rc for name, rc, _ in results}
        assert by_name["g1"] == 2
        assert all(rc == 0 for name, rc in by_name.items() if name != "g1")

    def test_worker_exception_recorded_as_failure(self):
        def run_one(name, paths, cancel):
            if name == "g2":
                raise RuntimeError("workspace cleanup failed")
            return 0, "ok\n"

        results = _run_groups_concurrently(self.GROUPS, run_one, jobs=3)
        by_name = {name: (rc, output) for name, rc, output in results}
        rc, output = by_name["g2"]
        assert rc == 1
        assert "workspace cleanup failed" in output

    def test_jobs_one_degenerates_to_sequential(self):
        order: list[str] = []

        def run_one(name, paths, cancel):
            order.append(name)
            return 0, ""

        _run_groups_concurrently(self.GROUPS, run_one, jobs=1)
        assert order == [g for g, _ in self.GROUPS]

    def test_output_is_returned_verbatim(self):
        def run_one(name, paths, cancel):
            return 0, f"line1-{name}\nline2-{name}\n"

        results = _run_groups_concurrently(self.GROUPS[:2], run_one, jobs=2)
        assert results[0][2] == "line1-g0\nline2-g0\n"
        assert results[1][2] == "line1-g1\nline2-g1\n"


class TestScanRepoBytecode:
    def test_pruned_walk_matches_expected_layout(self, tmp_path: Path):
        (tmp_path / "src" / "__pycache__").mkdir(parents=True)
        (tmp_path / "src" / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"x")
        (tmp_path / "stray.pyc").write_bytes(b"x")
        # Top-level runtime dirs must be pruned entirely.
        (tmp_path / "data" / "__pycache__").mkdir(parents=True)
        (tmp_path / "data" / "__pycache__" / "polluted.pyc").write_bytes(b"x")
        (tmp_path / "output").mkdir()
        (tmp_path / "output" / "cache.pyc").write_bytes(b"x")
        # A NESTED directory named data is still scanned (top-level-only prune).
        (tmp_path / "src" / "data").mkdir()
        (tmp_path / "src" / "data" / "nested.pyc").write_bytes(b"x")

        pycache, pyc = scan_repo_bytecode(tmp_path)
        assert pycache == ["src/__pycache__"]
        assert sorted(pyc) == [
            "src/__pycache__/mod.cpython-312.pyc",
            "src/data/nested.pyc",
            "stray.pyc",
        ]

    def test_clean_tree_returns_empty(self, tmp_path: Path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "mod.py").write_text("x = 1\n", encoding="utf-8")
        assert scan_repo_bytecode(tmp_path) == ([], [])


class TestChildEnvEncoding:
    def test_child_env_sets_pythonioencoding(self):
        with TestRuntimeWorkspace(group="unit_encoding_check") as ws:
            env = ws.child_env()
        assert env["PYTHONIOENCODING"] == "utf-8"
        assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
