from __future__ import annotations
import json
from pathlib import Path

import pytest

from src.catalog_folders.assignment import valid_decisions
from src.catalog_folders.formal_registry import FormalPaper
from src.catalog_folders.link_backend import create_paper_link, inspect_paper_link, remove_paper_link
from src.catalog_folders.models import CLASSIFIER_SKILL_VERSION, Category
from src.catalog_folders.registry import category_from_notebook, definition_hash, safe_keyword
from src.file_fingerprint import compute_sha256


def test_notebook_category_uses_only_chinese_identity_fields(tmp_path: Path):
    notebook=tmp_path/"风吹雪.json"; notebook.write_text(json.dumps({"keyword_id":"a1b2c3d4e5f6a7b8","keyword":"风吹雪","normalized_keyword":"风吹雪","expansions":[{"query":"ignored"}]},ensure_ascii=False),encoding="utf-8")
    category=category_from_notebook(notebook)
    assert category.directory_name=="风吹雪__a1b2c3d4" and category.source_notebook==notebook.name
    assert safe_keyword(" 风吹雪 ")=="风吹雪"


def test_assignment_positive_negative_and_hash_invalidation(tmp_path: Path):
    folder=tmp_path/"paper"; folder.mkdir(); catalog=folder/"paper.catalog.json"; catalog.write_text("{}",encoding="utf-8")
    paper=FormalPaper("0000000000000001","paper",folder,catalog,folder/"paper.metadata.json")
    base={"category_id":"a1b2c3d4e5f6a7b8","keyword_zh":"风吹雪","normalized_keyword_zh":"风吹雪"}
    category=Category(**base,directory_name="风吹雪__a1b2c3d4",source_notebook="x.json",definition_sha256=definition_hash(base))
    assignment={"schema_version":"1.0","paper_number":paper.paper_number,"paper_id":paper.paper_id,"catalog_sha256":compute_sha256(catalog),"decisions":{category.category_id:{"category_definition_sha256":category.definition_sha256,"matched":False,"classifier_skill_version":CLASSIFIER_SKILL_VERSION}}}
    assert valid_decisions(assignment,paper,[category])[category.category_id]["matched"] is False
    catalog.write_text('{"changed":true}',encoding="utf-8")
    assert valid_decisions(assignment,paper,[category])=={}


def test_directory_link_remove_never_deletes_target(tmp_path: Path):
    target=tmp_path/"papers"/"paper"; target.mkdir(parents=True); sentinel=target/"sentinel"; sentinel.write_text("safe",encoding="utf-8")
    link=tmp_path/"catalog"/"all"/"0000000000000001"; created=create_paper_link(link,target)
    assert inspect_paper_link(link).target==target.resolve() and created.kind in {"symlink","junction"}
    remove_paper_link(link)
    assert sentinel.read_text(encoding="utf-8")=="safe" and not link.exists()


def test_refuses_unmanaged_directory_removal(tmp_path: Path):
    path=tmp_path/"ordinary"; path.mkdir()
    with pytest.raises(ValueError): remove_paper_link(path)
