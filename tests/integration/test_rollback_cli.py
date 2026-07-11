"""Subprocess CLI tests for rollback_formal_papers_to_paper_raw.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.ingest.commit import commit_paper_raw
from src.ingest.formalization import write_formalization_plan
from tests.integration.test_frozen_v32_transaction_pipeline import _workspace

_ROOT = Path(__file__).parent.parent.parent
SCRIPT = str(_ROOT / "scripts" / "rollback_formal_papers_to_paper_raw.py")


def _run(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        capture_output=True, text=True, encoding="utf-8", cwd=str(_ROOT),
        timeout=60, env=env,
    )


def _committed(tmp_path: Path) -> dict:
    """Create a committed formal paper and return paths alongside result."""
    workspace, papers, ledger_path, catalog_root = _workspace(tmp_path)
    write_formalization_plan(workspace, papers_dir=papers)
    result = commit_paper_raw(
        workspace, paper_raw_root=tmp_path / "paper_raw", papers_dir=papers,
        ledger_path=ledger_path, catalog_root=catalog_root,
        transactions_dir=tmp_path / "transactions",
    )
    result["_ledger_path"] = str(ledger_path.resolve())
    result["_catalog_root_path"] = str(catalog_root.resolve())
    result["_papers_dir"] = str(papers.resolve())
    result["_paper_raw_root"] = str((tmp_path / "paper_raw").resolve())
    result["_transaction_root"] = str((tmp_path / "transactions").resolve())
    return result


def _paths(result: dict) -> list[str]:
    return [
        "--papers-dir", result["_papers_dir"],
        "--paper-raw-root", result["_paper_raw_root"],
        "--transaction-root", result["_transaction_root"],
        "--ledger-path", result["_ledger_path"],
        "--catalog-root", result["_catalog_root_path"],
    ]


# ── Test: CLI Entry ──────────────────────────────────────────────

class TestCliEntry:
    def test_help(self) -> None:
        cp = _run("--help")
        assert cp.returncode == 0
        assert "--paper-number" in cp.stdout
        assert "--paper-id" in cp.stdout
        assert "--all-papers" in cp.stdout

    def test_no_target(self) -> None:
        cp = _run()
        assert cp.returncode != 0
        assert "one of" in cp.stderr.lower()

    def test_mutually_exclusive(self) -> None:
        cp = _run("--paper-number", "0000000000000001", "--paper-id", "x")
        assert cp.returncode != 0

    def test_invalid_paper_number(self) -> None:
        cp = _run("--paper-number", "12345")
        assert cp.returncode != 0


# ── Test: --paper-number ─────────────────────────────────────────

class TestPaperNumberDryRun:
    def test_dry_run_no_mutation(self, tmp_path: Path) -> None:
        result = _committed(tmp_path)
        pn = result["paper_number"]
        pid = result["paper_id"]

        cp = _run("--paper-number", pn, *_paths(result))
        assert cp.returncode == 0, f"stdout={cp.stdout}"
        assert "DRY-RUN" in cp.stdout
        assert (Path(result["_papers_dir"]) / pid / f"{pid}.pdf").exists()

    def test_report_valid(self, tmp_path: Path) -> None:
        result = _committed(tmp_path)
        pn = result["paper_number"]
        rp = tmp_path / "report.json"
        cp = _run("--paper-number", pn, *_paths(result), "--report", str(rp))
        assert cp.returncode == 0
        report = json.loads(rp.read_text(encoding="utf-8"))
        assert report["mode"] == "dry_run"
        assert report["requested_target"]["paper_number"] == pn


class TestPaperNumberApply:
    def test_apply_completes(self, tmp_path: Path) -> None:
        result = _committed(tmp_path)
        pn = result["paper_number"]
        pid = result["paper_id"]
        papers_dir = Path(result["_papers_dir"])
        paper_raw = Path(result["_paper_raw_root"])

        cp = _run("--paper-number", pn, "--apply", *_paths(result))
        assert cp.returncode == 0, f"stdout={cp.stdout} stderr={cp.stderr}"
        assert "completed" in cp.stdout
        assert not (papers_dir / pid).exists()
        raw = paper_raw / pn
        assert raw.is_dir()
        assert (raw / f"{pn}.pdf").exists()
        markers = list(raw.glob("*.paper.number"))
        assert len(markers) == 1

    def test_non_active_fails(self, tmp_path: Path) -> None:
        r = _committed(tmp_path)
        cp = _run("--paper-number", "0000000000000099", "--apply", *_paths(r))
        assert cp.returncode != 0

    def test_blocking_writes_report(self, tmp_path: Path) -> None:
        r = _committed(tmp_path)
        rp = tmp_path / "report.json"
        cp = _run("--paper-number", "0000000000000099", "--apply",
                   *_paths(r), "--report", str(rp))
        assert cp.returncode != 0
        assert rp.exists()
        report = json.loads(rp.read_text(encoding="utf-8"))
        assert report["summary"]["failed"] >= 1

    def test_report_on_success(self, tmp_path: Path) -> None:
        result = _committed(tmp_path)
        pn = result["paper_number"]
        rp = tmp_path / "report.json"
        cp = _run("--paper-number", pn, "--apply", *_paths(result),
                   "--report", str(rp))
        assert cp.returncode == 0
        report = json.loads(rp.read_text(encoding="utf-8"))
        assert report["summary"]["completed"] == 1
        assert report["summary"]["failed"] == 0


# ── Test: --paper-id ─────────────────────────────────────────────

class TestPaperId:
    def test_resolves_active(self, tmp_path: Path) -> None:
        result = _committed(tmp_path)
        pid = result["paper_id"]
        cp = _run("--paper-id", pid, *_paths(result))
        assert cp.returncode == 0, f"stdout={cp.stdout} stderr={cp.stderr}"
        assert "DRY-RUN" in cp.stdout

    def test_not_found(self, tmp_path: Path) -> None:
        r = _committed(tmp_path)
        cp = _run("--paper-id", "nonexistent_12345", *_paths(r))
        assert cp.returncode != 0
        assert ("not_found" in (cp.stdout + cp.stderr).lower()
                or "paper_id" in (cp.stdout + cp.stderr).lower())

    def test_recovers_interrupted_journal(self, tmp_path: Path) -> None:
        result = _committed(tmp_path)
        pn = result["paper_number"]
        pid = result["paper_id"]
        papers_dir = Path(result["_papers_dir"])
        trans_root = Path(result["_transaction_root"])

        from src.ingest.rollback import rollback_formal_papers

        raised = [False]
        def crash_once(phase: str) -> None:
            if phase == "formal_quarantined" and not raised[0]:
                raised[0] = True
                raise RuntimeError("injected crash")

        with pytest.raises(RuntimeError, match="injected crash"):
            rollback_formal_papers(
                papers_dir=papers_dir,
                paper_raw_root=Path(result["_paper_raw_root"]),
                transaction_root=trans_root,
                ledger_path=Path(result["_ledger_path"]),
                catalog_root=Path(result["_catalog_root_path"]),
                paper_number=pn,
                fault_injector=crash_once,
            )

        cp = _run("--paper-id", pid, "--apply", *_paths(result))
        assert cp.returncode == 0, f"stdout={cp.stdout} stderr={cp.stderr}"
        assert not (papers_dir / pid).exists()

    def test_rejects_path_separators(self) -> None:
        cp = _run("--paper-id", "../escape")
        assert cp.returncode != 0

    def test_dry_run_resolves(self, tmp_path: Path) -> None:
        result = _committed(tmp_path)
        pid = result["paper_id"]
        rp = tmp_path / "report.json"
        cp = _run("--paper-id", pid, *_paths(result), "--report", str(rp))
        assert cp.returncode == 0
        report = json.loads(rp.read_text(encoding="utf-8"))
        assert report["requested_target"]["paper_id"] == pid


# ── Test: --all-papers ───────────────────────────────────────────

class TestAllPapers:
    def test_dry_run_discovers(self, tmp_path: Path) -> None:
        result = _committed(tmp_path)
        rp = tmp_path / "report.json"
        cp = _run("--all-papers", *_paths(result), "--report", str(rp))
        assert cp.returncode == 0, f"stdout={cp.stdout} stderr={cp.stderr}"
        report = json.loads(rp.read_text(encoding="utf-8"))
        assert report["summary"]["discovered"] >= 1
        assert report["summary"]["planned"] >= 1

    def test_dry_run_no_mutation(self, tmp_path: Path) -> None:
        result = _committed(tmp_path)
        pid = result["paper_id"]
        papers_dir = Path(result["_papers_dir"])
        cp = _run("--all-papers", *_paths(result))
        assert cp.returncode == 0
        assert (papers_dir / pid / f"{pid}.pdf").exists()

    def test_apply_completes_all(self, tmp_path: Path) -> None:
        result = _committed(tmp_path)
        pid = result["paper_id"]
        papers_dir = Path(result["_papers_dir"])
        rp = tmp_path / "report.json"
        cp = _run("--all-papers", "--apply", *_paths(result),
                   "--report", str(rp))
        assert cp.returncode == 0, f"stdout={cp.stdout} stderr={cp.stderr}"
        report = json.loads(rp.read_text(encoding="utf-8"))
        assert report["summary"]["completed"] >= 1
        assert report["summary"]["failed"] == 0
        assert not (papers_dir / pid).exists()

    def test_blocking_stops_apply(self, tmp_path: Path) -> None:
        papers_dir = tmp_path / "papers"
        papers_dir.mkdir(parents=True, exist_ok=True)
        bogus = papers_dir / "bogus_no_marker"
        bogus.mkdir()
        (bogus / "something.txt").write_text("nothing")
        catalog_dir = tmp_path / "catalog"
        catalog_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = catalog_dir / "paper_number_ledger.json"
        ledger_path.write_text(
            json.dumps({"schema_version": "1.0", "max_number": "0000000000000000", "items": {}}))

        rp = tmp_path / "report.json"
        cp = _run("--all-papers", "--apply",
                   "--papers-dir", str(papers_dir),
                   "--paper-raw-root", str(tmp_path / "paper_raw"),
                   "--transaction-root", str(tmp_path / "transactions"),
                   "--ledger-path", str(ledger_path),
                   "--catalog-root", str(catalog_dir),
                   "--report", str(rp))
        assert cp.returncode != 0
        assert rp.exists()
        report = json.loads(rp.read_text(encoding="utf-8"))
        assert report["summary"]["blocking_errors"] >= 1
