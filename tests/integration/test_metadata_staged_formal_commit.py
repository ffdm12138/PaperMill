"""Formal activation accepts only metadata_staged lifecycle state."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.library.paper_number_ledger import LEDGER_METADATA_STAGED, LEDGER_RESERVED, PaperNumberLedger
from src.library.paper_number_state import InvalidLedgerTransition, assert_ledger_transition
from src.ingest.import_status import STAGE_FAILED, write_import_status
from tests.factories.paper_raw_factory import (
    create_active_formal_workspace,
    create_metadata_staged_network_workspace,
    create_reserved_network_workspace,
)


pytestmark = pytest.mark.integration


def test_activate_from_reserved_is_rejected(tmp_path: Path):
    folder = create_reserved_network_workspace(tmp_path)
    ledger = PaperNumberLedger(tmp_path / "ledger.json")

    with pytest.raises(InvalidLedgerTransition, match="repair_required_reserved_final_mismatch"):
        ledger.activate_metadata_staged(folder.name, tmp_path / "papers" / "final")


def test_activate_from_metadata_staged_succeeds(tmp_path: Path):
    folder = create_metadata_staged_network_workspace(tmp_path)
    ledger = PaperNumberLedger(tmp_path / "ledger.json")

    ledger.activate_metadata_staged(folder.name, tmp_path / "papers" / "final", paper_name="Test")

    assert ledger.load()["items"][folder.name]["state"] == "active"


@pytest.mark.parametrize("state", ["allocating", "abandoned"])
def test_activate_from_other_state_is_rejected(state: str):
    with pytest.raises(InvalidLedgerTransition):
        assert_ledger_transition(
            paper_number="0000000000000001", current_state=state, target_state="active")


def test_rollback_active_to_metadata_staged(tmp_path: Path):
    formal = create_active_formal_workspace(tmp_path)
    ledger = PaperNumberLedger(tmp_path / "ledger.json")
    number = next(iter(ledger.load()["items"]))
    raw_target = tmp_path / "paper_raw" / number

    ledger.rollback_active_to_metadata_staged(number, raw_target)

    assert ledger.load()["items"][number]["state"] == LEDGER_METADATA_STAGED


def test_stage_failed_is_import_status_not_ledger_state(tmp_path: Path):
    folder = create_reserved_network_workspace(tmp_path)
    ledger = PaperNumberLedger(tmp_path / "ledger.json")

    result = write_import_status(
        folder, STAGE_FAILED, reason="test error", errors=["test error"],
        extra={"paper_number": folder.name, "paper_raw_id": folder.name},
    )

    assert ledger.load()["items"][folder.name]["state"] == LEDGER_RESERVED
    assert result["status"] == "stage_failed"
    assert isinstance(json.loads((folder / ".import_status.json").read_text(encoding="utf-8")), dict)
