import json
from pathlib import Path

import pytest

from scripts.check_directory_hygiene import check_directory_hygiene

pytestmark=pytest.mark.hygiene


def test_hygiene_rejects_retired_catalog_index_paths(tmp_path: Path):
    root=tmp_path/"data"/"catalog"; root.mkdir(parents=True)
    (root/("all"+".catalog.json")).write_text("{}",encoding="utf-8")
    report=check_directory_hygiene(project_root=tmp_path,papers_dir=tmp_path/"data"/"papers",paper_raw_dir=tmp_path/"data"/"paper_raw",catalog_root=root,ledger_path=root/"paper_number_ledger.json")
    assert not report["ok"] and any("retired" in error for error in report["errors"])


def test_hygiene_reports_dirty_without_deleting_marker(tmp_path: Path):
    root=tmp_path/"data"/"catalog"; (root/".state").mkdir(parents=True); (root/"all").mkdir(); (root/"_pending").mkdir(); marker=root/".state"/"DIRTY"; marker.write_text("crash",encoding="utf-8")
    report=check_directory_hygiene(project_root=tmp_path,papers_dir=tmp_path/"data"/"papers",paper_raw_dir=tmp_path/"data"/"paper_raw",catalog_root=root,ledger_path=root/"paper_number_ledger.json")
    assert not report["ok"] and marker.read_text(encoding="utf-8")=="crash"
