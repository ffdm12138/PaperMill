from __future__ import annotations

import pytest

from tests.factories.paper_raw_factory import make_staged_source


pytestmark = pytest.mark.e2e


def test_mocked_converted_workspace_has_current_manifest(tmp_path):
    folder = make_staged_source(tmp_path)
    paper_number = folder.name

    assert (folder / f"{paper_number}.conversion.json").exists()
    assert (folder / f"{paper_number}.md").exists()
    assert (folder / "images").is_dir()
