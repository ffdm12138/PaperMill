import json
from pathlib import Path

from scripts.validate_rolled_back_paper_raw import validate_rolled_back_state
from src.services.v2_library import PaperNumberLedger
from tests.helpers.paper_raw_factory import make_staged_source


def _empty_indexes(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    catalog.mkdir(parents=True, exist_ok=True)
    (catalog / "all.catalog.json").write_text(
        json.dumps({"schema_version": "3.0", "updated_at": "", "papers": []}),
        encoding="utf-8",
    )
    (catalog / "paper_index.json").write_text(
        json.dumps({
            "schema_version": "2.0",
            "description": "Path index only; bibliographic facts stay in metadata.json.",
            "updated_at": "",
            "papers": [],
        }),
        encoding="utf-8",
    )


def test_validate_rolled_back_state_accepts_clean_raw(tmp_path):
    number = "0000000000000001"
    folder = make_staged_source(tmp_path, number)
    (folder / f"{number}.catalog.json").unlink()
    _empty_indexes(tmp_path)

    errors, warnings, states = validate_rolled_back_state(
        papers_dir=tmp_path / "papers",
        paper_raw_dir=tmp_path / "paper_raw",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
    )

    assert errors == []
    assert states[0]["schema_valid"] is True
    assert states[0]["citation_ready"] is True
    assert warnings == []


def test_validate_rolled_back_state_rejects_matched_not_citation_ready(tmp_path):
    number = "0000000000000001"
    folder = make_staged_source(tmp_path, number)
    (folder / f"{number}.catalog.json").unlink()
    meta_path = folder / f"{number}.metadata.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    metadata["authors"] = []
    metadata["metadata_match"]["status"] = "matched"
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    _empty_indexes(tmp_path)

    errors, _, states = validate_rolled_back_state(
        papers_dir=tmp_path / "papers",
        paper_raw_dir=tmp_path / "paper_raw",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
    )

    assert any("metadata marked matched but not citation-ready" in err for err in errors)
    assert states[0]["matched_consistent"] is False


def test_validate_rolled_back_state_rejects_non_empty_indexes_and_active_ledger(tmp_path):
    number = "0000000000000001"
    folder = make_staged_source(tmp_path, number)
    (folder / f"{number}.catalog.json").unlink()
    ledger = PaperNumberLedger(tmp_path / "catalog" / "paper_number_ledger.json")
    data = ledger.load()
    data["items"][number]["state"] = "active"
    ledger.save(data)
    catalog = tmp_path / "catalog"
    catalog.mkdir(parents=True, exist_ok=True)
    (catalog / "all.catalog.json").write_text(
        json.dumps({"schema_version": "3.0", "papers": [{"paper_number": number}]}),
        encoding="utf-8",
    )

    errors, _, _ = validate_rolled_back_state(
        papers_dir=tmp_path / "papers",
        paper_raw_dir=tmp_path / "paper_raw",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
    )

    assert any("ledger state must be reserved" in err for err in errors)
    assert any("all.catalog.papers must be empty" in err for err in errors)


def test_validate_rolled_back_state_rejects_non_numbered_workspace(tmp_path):
    folder = tmp_path / "paper_raw" / "old_formalized"
    folder.mkdir(parents=True)
    (folder / "old_formalized.metadata.json").write_text("{}", encoding="utf-8")
    (folder / "old_formalized.md").write_text("# old", encoding="utf-8")
    (folder / "old_formalized.pdf").write_bytes(b"%PDF")
    (folder / "images").mkdir()
    _empty_indexes(tmp_path)

    errors, _, _ = validate_rolled_back_state(
        papers_dir=tmp_path / "papers",
        paper_raw_dir=tmp_path / "paper_raw",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
    )

    assert any("non-numbered paper_raw workspace remains after full rollback" in err for err in errors)


def test_active_ledger_item_after_rollback_is_error(tmp_path):
    number = "0000000000000001"
    folder = make_staged_source(tmp_path, number)
    (folder / f"{number}.catalog.json").unlink()
    _empty_indexes(tmp_path)

    # Force the ledger item to active — this should be caught.
    ledger = PaperNumberLedger(tmp_path / "catalog" / "paper_number_ledger.json")
    data = ledger.load()
    data["items"][number]["state"] = "active"
    ledger.save(data)

    errors, _, _ = validate_rolled_back_state(
        papers_dir=tmp_path / "papers",
        paper_raw_dir=tmp_path / "paper_raw",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
    )

    assert any("active item remains after rollback" in err for err in errors)


def test_reserved_orphan_ledger_item_is_warning(tmp_path):
    number = "0000000000000001"
    _empty_indexes(tmp_path)

    # Ledger has a reserved item pointing at a nonexistent folder (orphan).
    ledger = PaperNumberLedger(tmp_path / "catalog" / "paper_number_ledger.json")
    data = ledger.empty_data()
    data["max_number"] = number
    data["items"][number] = {
        "folder_name": number,
        "folder_path": str(tmp_path / "paper_raw" / number),
        "state": "reserved",
        "created_at": "2026-01-01T00:00:00",
    }
    ledger.save(data)

    errors, warnings, _ = validate_rolled_back_state(
        papers_dir=tmp_path / "papers",
        paper_raw_dir=tmp_path / "paper_raw",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
    )

    assert not errors
    assert any(f"reserved orphan (no paper_raw folder): {number}" in w for w in warnings)


def test_inconsistent_raw_folders_and_ledger_items_reported(tmp_path):
    """Folder without ledger item + ledger item without folder — both reported."""
    number_a = "0000000000000001"
    number_b = "0000000000000002"
    # Folder A exists but no ledger item.
    folder_a = make_staged_source(tmp_path, number_a)
    (folder_a / f"{number_a}.catalog.json").unlink()
    _empty_indexes(tmp_path)

    # Remove ledger item for A, add orphan reserved item for B.
    ledger = PaperNumberLedger(tmp_path / "catalog" / "paper_number_ledger.json")
    data = ledger.load()
    del data["items"][number_a]
    data["max_number"] = number_b
    data["items"][number_b] = {
        "folder_name": number_b,
        "folder_path": str(tmp_path / "paper_raw" / number_b),
        "state": "reserved",
        "created_at": "2026-01-01T00:00:00",
    }
    ledger.save(data)

    errors, warnings, _ = validate_rolled_back_state(
        papers_dir=tmp_path / "papers",
        paper_raw_dir=tmp_path / "paper_raw",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
    )

    assert any(f"{number_a}: ledger item missing" in err for err in errors)
    assert any(f"reserved orphan (no paper_raw folder): {number_b}" in w for w in warnings)


def test_validate_rolled_back_state_rejects_invalid_ledger_key(tmp_path):
    """Non-16-digit ledger key after rollback must be an error, not silently skipped."""
    _empty_indexes(tmp_path)

    ledger = PaperNumberLedger(tmp_path / "catalog" / "paper_number_ledger.json")
    data = ledger.empty_data()
    data["max_number"] = "0000000000000001"
    data["items"] = {
        "not-a-number": {
            "folder_name": "bad",
            "folder_path": str(tmp_path / "paper_raw" / "bad"),
            "state": "reserved",
            "created_at": "2026-01-01T00:00:00",
        },
    }
    ledger.save(data)

    errors, warnings, _ = validate_rolled_back_state(
        papers_dir=tmp_path / "papers",
        paper_raw_dir=tmp_path / "paper_raw",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
    )

    assert any("invalid paper_number key after rollback" in err for err in errors)
    assert not any("invalid paper_number key after rollback" in w for w in warnings)


def test_validate_rolled_back_state_rejects_non_object_ledger_item(tmp_path):
    """Non-dict ledger item must be an error, not an AttributeError crash."""
    _empty_indexes(tmp_path)

    ledger = PaperNumberLedger(tmp_path / "catalog" / "paper_number_ledger.json")
    data = ledger.empty_data()
    data["max_number"] = "0000000000000001"
    data["items"] = {
        "0000000000000001": "not-a-dict",
    }
    ledger.save(data)

    errors, warnings, _ = validate_rolled_back_state(
        papers_dir=tmp_path / "papers",
        paper_raw_dir=tmp_path / "paper_raw",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
    )

    assert any("item must be object: 0000000000000001" in err for err in errors)
