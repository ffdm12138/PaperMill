import json

import scripts.validate_metadata_only_assets as validator
from src.services.v2_library import empty_metadata
from tests.helpers.paper_raw_factory import make_staged_source


def _write_rolled_back_workspace(tmp_path, paper_number, metadata):
    folder = make_staged_source(tmp_path, paper_number)
    catalog = folder / f"{paper_number}.catalog.json"
    if catalog.exists():
        catalog.unlink()
    (folder / f"{paper_number}.metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False),
        encoding="utf-8",
    )
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    (catalog_dir / "all.catalog.json").write_text(
        json.dumps({"schema_version": "3.1", "updated_at": "", "papers": []}),
        encoding="utf-8",
    )
    (catalog_dir / "paper_index.json").write_text(
        json.dumps({"schema_version": "2.0", "updated_at": "", "papers": []}),
        encoding="utf-8",
    )


def test_validate_metadata_only_assets_errors_on_matched_not_citation_ready(tmp_path, monkeypatch, capsys):
    paper_number = "0000000000000001"
    metadata = empty_metadata(paper_number, source_type="network_search")
    metadata["title"]["original"] = "Incomplete"
    metadata["year"] = 2024
    metadata["identifiers"]["doi"] = "10.1000/incomplete"
    metadata["metadata_match"]["status"] = "matched"
    _write_rolled_back_workspace(tmp_path, paper_number, metadata)

    monkeypatch.setattr(validator, "PAPERS_DIR", tmp_path / "papers")
    monkeypatch.setattr(validator, "PAPER_RAW_DIR", tmp_path / "paper_raw")
    monkeypatch.setattr(validator, "ALL_CATALOG_PATH", tmp_path / "catalog" / "all.catalog.json")
    monkeypatch.setattr(validator, "PAPER_NUMBER_LEDGER_PATH", tmp_path / "catalog" / "paper_number_ledger.json")

    rc = validator.main()
    out = capsys.readouterr().out

    assert rc == 1
    assert "schema_valid=True" in out
    assert "citation_ready=False" in out
    assert "matched_consistent=False" in out
    assert "metadata marked matched but not citation-ready" in out


def test_validate_metadata_only_assets_warns_on_schema_valid_not_citation_ready(tmp_path, monkeypatch, capsys):
    paper_number = "0000000000000001"
    metadata = empty_metadata(paper_number, source_type="network_search")
    metadata["title"]["original"] = "Incomplete"
    metadata["year"] = 2024
    metadata["identifiers"]["doi"] = "10.1000/incomplete"
    metadata["metadata_match"]["status"] = "unmatched"
    _write_rolled_back_workspace(tmp_path, paper_number, metadata)

    monkeypatch.setattr(validator, "PAPERS_DIR", tmp_path / "papers")
    monkeypatch.setattr(validator, "PAPER_RAW_DIR", tmp_path / "paper_raw")
    monkeypatch.setattr(validator, "ALL_CATALOG_PATH", tmp_path / "catalog" / "all.catalog.json")
    monkeypatch.setattr(validator, "PAPER_NUMBER_LEDGER_PATH", tmp_path / "catalog" / "paper_number_ledger.json")

    rc = validator.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "schema_valid=True" in out
    assert "citation_ready=False" in out
    assert "matched_consistent=True" in out
    assert "metadata schema valid but citation not ready" in out
