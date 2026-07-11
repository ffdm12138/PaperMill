from __future__ import annotations
import json
from pathlib import Path
import jsonschema,pytest

from src.catalog.freeze import assert_catalog_frozen
from src.catalog.schema import load_catalog_schema,validate_catalog_v32
from tests.integration.test_frozen_v32_transaction_pipeline import NUMBER,_workspace

def test_skill_example_matches_authoritative_v32_schema():
    example=json.loads(Path("skills/paper_raw_catalog_curator/examples/example_catalog.json").read_text(encoding="utf-8")); jsonschema.validate(example,load_catalog_schema()); assert example["schema_version"]=="3.2" and example["abstract"]["summary_zh"]

def test_catalog_rejects_recursive_bibliographic_mirror(tmp_path: Path):
    workspace,papers,_,_=_workspace(tmp_path); value=json.loads(workspace.catalog.read_text(encoding="utf-8")); value["writing_value"]["doi"]="10.1/x"
    errors=validate_catalog_v32(value,workspace.root,NUMBER,papers_dir=papers,paper_raw_root=workspace.root.parent); assert any("forbidden" in error or "Additional properties" in error for error in errors)

def test_catalog_task_input_change_makes_freeze_stale(tmp_path: Path):
    workspace,papers,_,_=_workspace(tmp_path); workspace.markdown.write_text(workspace.markdown.read_text(encoding="utf-8")+"\nchanged",encoding="utf-8")
    with pytest.raises(ValueError): assert_catalog_frozen(workspace.root,NUMBER,papers_dir=papers,paper_raw_root=workspace.root.parent)
