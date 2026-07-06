from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import scripts.match_paper_raw_metadata as legacy
from src.services.metadata_resolver import STATUS_CANDIDATE_CONFLICT
from src.services.v2_library import empty_metadata

pytestmark = pytest.mark.legacy


class FakeReport:
    def __init__(self, *, decision: str = "manual_review"):
        self.source_id = "0000000000000001"
        self.decision = decision
        self.best_candidate_id = "cand_001"
        self.candidates = []
        self.doi_source = "network_title"
        self.warnings = []
        self.reason = "test"
        self.created_at = "2026-01-01T00:00:00"


def _make_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "paper_raw" / "0000000000000001"
    folder.mkdir(parents=True)
    (folder / "0000000000000001.pdf").write_bytes(b"%PDF")
    (folder / "0000000000000001.md").write_text("# Test Title", encoding="utf-8")
    (folder / "0000000000000001.metadata.json").write_text(
        json.dumps(empty_metadata("0000000000000001"), ensure_ascii=False),
        encoding="utf-8",
    )
    return folder


def _run(argv: list[str]) -> int:
    saved = sys.argv
    sys.argv = argv
    try:
        return legacy.main()
    finally:
        sys.argv = saved


def test_legacy_script_calls_canonical_resolver(monkeypatch, tmp_path, capsys):
    _make_folder(tmp_path)
    calls = {"resolver": 0}

    def fake_resolve(folder, **kwargs):
        calls["resolver"] += 1
        assert Path(folder).name == "0000000000000001"
        assert kwargs["allow_network"] is True
        return FakeReport(decision="manual_review")

    monkeypatch.setattr(legacy, "resolve_metadata_candidates", fake_resolve)

    rc = _run([
        "match_paper_raw_metadata.py",
        "--paper-number", "0000000000000001",
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
    ])

    captured = capsys.readouterr()
    assert rc == 0
    assert calls["resolver"] == 1
    assert legacy.LEGACY_NOTICE in captured.err
    assert json.loads(captured.out)["legacy_wrapper"] is True


def test_legacy_script_does_not_contain_old_direct_enrichment_logic():
    text = Path(legacy.__file__).read_text(encoding="utf-8")
    assert "enrich_from_pdf" not in text
    assert "_has_" + "bibliographic_identity" not in text


def test_legacy_conflict_writes_status_without_applying(monkeypatch, tmp_path):
    folder = _make_folder(tmp_path)
    monkeypatch.setattr(
        legacy,
        "resolve_metadata_candidates",
        lambda *a, **k: FakeReport(decision="conflict"),
    )
    monkeypatch.setattr(legacy, "write_candidates_json", lambda *a, **k: None)
    monkeypatch.setattr(legacy, "write_resolve_report_json", lambda *a, **k: None)
    monkeypatch.setattr(legacy, "write_metadata_patch_json", lambda *a, **k: None)
    monkeypatch.setattr(
        legacy,
        "apply_resolution",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("conflict must not apply")),
    )

    rc = _run([
        "match_paper_raw_metadata.py",
        "--paper-number", "0000000000000001",
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--apply",
    ])

    status = json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))
    metadata = json.loads((folder / "0000000000000001.metadata.json").read_text(encoding="utf-8"))
    assert rc == 0
    assert status["status"] == STATUS_CANDIDATE_CONFLICT
    assert metadata["metadata_match"]["status"] == "unmatched"


def test_legacy_require_matched_fails_for_manual_review(monkeypatch, tmp_path):
    _make_folder(tmp_path)
    monkeypatch.setattr(
        legacy,
        "resolve_metadata_candidates",
        lambda *a, **k: FakeReport(decision="manual_review"),
    )

    rc = _run([
        "match_paper_raw_metadata.py",
        "--paper-number", "0000000000000001",
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--require-matched",
    ])

    assert rc == 1

