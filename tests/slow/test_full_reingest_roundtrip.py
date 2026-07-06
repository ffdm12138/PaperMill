import json
from pathlib import Path

import pytest

from scripts.rollback_formal_papers_to_paper_raw import rollback_formal_papers
from scripts.validate_v2_library import validate_v2_library
from src.services.asset_manifest import read_asset_manifest
from src.services.paper_raw_formalizer import PaperRawFormalizationService
from src.services.v2_library import PaperCurationService, V2PaperCommitService, empty_catalog
from tests.helpers.paper_raw_factory import commit_for_test, fill_valid_catalog_v31, formalize_for_test, make_staged_source


def _fill_catalog(title: str) -> dict:
    return fill_valid_catalog_v31(
        empty_catalog(),
        paper_number="0000000000000001",
        title_zh=title,
        title_original="Trusted Original",
        domain="blowing_snow",
    )


def test_full_rollback_reingest_roundtrip_preserves_paper_number(tmp_path):
    source = make_staged_source(tmp_path, "0000000000000001", title_zh="旧标题")
    formalized = formalize_for_test(
        tmp_path,
        source,
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
    )
    assert formalized["success"], formalized
    first_commit = commit_for_test(
        tmp_path,
        Path(formalized["folder"]),
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
    )
    assert first_commit["status"] == "imported"
    number = first_commit["paper_number"]

    rollback = rollback_formal_papers(
        papers_dir=tmp_path / "papers",
        paper_raw_dir=tmp_path / "paper_raw",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        archive_dir=tmp_path / "transactions" / "rollback",
        all_papers=True,
        apply=True,
    )
    assert rollback["summary"]["rolled_back"] == 1
    raw = tmp_path / "paper_raw" / number
    assert raw.exists()
    assert not list(raw.glob("*.catalog.json"))

    catalog_path = raw / f"{number}.catalog.json"
    catalog_path.write_text(json.dumps(_fill_catalog("新标题"), ensure_ascii=False), encoding="utf-8")
    curated = PaperCurationService().apply_curated_files(raw, curated_catalog_path=catalog_path)
    assert curated["success"], curated

    formalized_again = PaperRawFormalizationService(
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
    ).formalize(raw)
    assert formalized_again["success"], formalized_again
    assert formalized_again["paper_number"] == number

    final_commit = V2PaperCommitService(
        papers_dir=tmp_path / "papers",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
    ).commit_paper_raw(formalized_again["folder"])
    assert final_commit["status"] == "imported"
    assert final_commit["paper_number"] == number

    final = tmp_path / "papers" / final_commit["paper_id"]
    manifest = read_asset_manifest(final, final_commit["paper_id"])
    assert manifest["paper_number"] == number
    assert manifest["paper_id"] == final_commit["paper_id"]
    assert manifest["stage"] == "papers"

    errors, warnings = validate_v2_library(
        papers_dir=tmp_path / "papers",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        check_paths=False,
    )
    assert errors == []
    assert isinstance(warnings, list)
