"""Contracts for discovery's lock-owned ledger session."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.discovery.staging_metrics import CollectingStagingMetricsObserver
from src.library.paper_number_ledger import LockedLedgerSession, PaperNumberLedger


def _ledger(tmp_path: Path) -> PaperNumberLedger:
    return PaperNumberLedger(tmp_path / "ledger.json")


def test_locked_session_loads_once_and_uses_two_durable_checkpoints(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    observer = CollectingStagingMetricsObserver()
    ledger = _ledger(tmp_path)

    with LockedLedgerSession(ledger, observer=observer) as session:
        number, folder = session.reserve_number(paper_raw)
        assert ledger.load()["items"][number]["state"] == "reserved"
        session.transition_metadata_staged(number, folder)
        session.save_checkpoint()

    assert observer.ledger_loads == 1
    assert observer.ledger_saves == 2
    assert ledger.load()["items"][number]["state"] == "metadata_staged"


def test_locked_session_reserve_never_scans_paper_raw(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)

    def boom(*args, **kwargs):
        raise AssertionError("locked discovery reserve must not scan the workspace tree")

    monkeypatch.setattr(PaperNumberLedger, "scan_paper_raw_number_floor", boom)
    with LockedLedgerSession(ledger) as session:
        number, folder = session.reserve_number(tmp_path / "paper_raw")

    assert number == "0000000000000001"
    assert folder.is_dir()


def test_locked_session_reuses_loaded_counter_for_multiple_reservations(tmp_path):
    ledger = _ledger(tmp_path)
    with LockedLedgerSession(ledger) as session:
        first, _ = session.reserve_number(tmp_path / "paper_raw")
        second, _ = session.reserve_number(tmp_path / "paper_raw")

    assert (first, second) == ("0000000000000001", "0000000000000002")
    assert ledger.load()["max_number"] == second


def test_locked_session_never_overwrites_existing_number_when_counter_regresses(tmp_path):
    ledger = _ledger(tmp_path)
    number = "0000000000000001"
    data = ledger.empty_data()
    data["items"][number] = {
        "state": "abandoned", "folder_name": number, "folder_path": "",
        "abandoned_reason": "historical allocation",
    }
    ledger.save(data)
    before = ledger.path.read_bytes()

    with LockedLedgerSession(ledger) as session:
        with pytest.raises(RuntimeError, match="paper_number_counter_collision"):
            session.reserve_number(tmp_path / "paper_raw")

    assert ledger.path.read_bytes() == before
    assert not (tmp_path / "paper_raw" / number).exists()
