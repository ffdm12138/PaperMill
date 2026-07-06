from __future__ import annotations

import pytest

from tests.factories.paper_raw_factory import commit_for_test, formalize_for_test, make_staged_source


pytestmark = pytest.mark.integration


def test_paper_raw_formalize_then_commit_uses_tmp_catalog_paths(tmp_path):
    folder = make_staged_source(tmp_path, title_zh="可信论文", title_original="Trusted Paper")

    formalized = formalize_for_test(tmp_path, folder)
    formalized_folder = tmp_path / "paper_raw" / formalized["paper_id"]
    committed = commit_for_test(tmp_path, formalized_folder)

    assert formalized["paper_number"] == "0000000000000001"
    assert committed["status"] == "imported"
    assert (tmp_path / "papers" / formalized["paper_id"]).exists()
    assert (tmp_path / "catalog" / "paper_number_ledger.json").exists()
    assert (tmp_path / "catalog" / "all.catalog.json").exists()


# ── Source record strict gate tests ────────────────────────────────────

def test_formalize_rejects_empty_metadata_source_record_path(tmp_path):
    import json

    folder = make_staged_source(tmp_path, title_zh="可信论文")
    paper_number = folder.name
    metadata_path = folder / f"{paper_number}.metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["source"]["raw_record_path"] = ""
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")

    result = formalize_for_test(tmp_path, folder)

    assert not result["success"]
    assert result["status"] == "formalize_failed"
    assert any("source.raw_record_path is required" in err for err in result["errors"])


def test_formalize_rejects_missing_metadata_source_record_file(tmp_path):
    import json

    folder = make_staged_source(tmp_path, title_zh="可信论文")
    paper_number = folder.name
    metadata_path = folder / f"{paper_number}.metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    (folder / metadata["source"]["raw_record_path"]).unlink()

    result = formalize_for_test(tmp_path, folder)

    assert not result["success"]
    assert result["status"] == "formalize_failed"
    assert any(
        "source.raw_record_path" in err and "does not exist" in err
        for err in result["errors"]
    )


def test_commit_readiness_rejects_empty_metadata_source_record_path(tmp_path):
    import json

    from src.services.v2_library import assess_paper_raw_commit_readiness

    folder = make_staged_source(tmp_path, title_zh="可信论文")
    paper_number = folder.name
    metadata_path = folder / f"{paper_number}.metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["source"]["raw_record_path"] = ""
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")

    result = assess_paper_raw_commit_readiness(
        str(folder), file_prefix=paper_number, paper_id="2026_Test_可信论文",
    )

    assert result["status"] != "ready"
    assert any(
        "source.raw_record_path is required" in err for err in result.get("errors", [])
    )
