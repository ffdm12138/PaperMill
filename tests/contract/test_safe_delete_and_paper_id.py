import pytest

from src.services.paper_id import generate_paper_id
from src.utils.safe_delete import SafeDeleteError, safe_delete_duplicate_artifact
from src.writer.bib_manager import validate_catalog_citations


def test_generate_paper_id_preserves_chinese_and_author():
    pid = generate_paper_id(
        year=1982,
        authors=[{"family": "Schmidt"}],
        title="Vertical profiles of blowing snow",
        chinese_title="风吹雪垂直剖面",
    )
    assert pid == "1982_schmidt_风吹雪垂直剖面"


def test_generate_paper_id_cleans_windows_unsafe_chars():
    pid = generate_paper_id(year=2024, title='A/B:C*D?"E<F>|G')
    assert "/" not in pid and ":" not in pid and "*" not in pid


def test_safe_delete_requires_duplicate_confirmation(tmp_path):
    target = tmp_path / "data" / "raw" / "dup.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"%PDF")
    with pytest.raises(SafeDeleteError):
        safe_delete_duplicate_artifact(target, data_root=tmp_path / "data", confirmed_duplicate=False)
    assert target.exists()


def test_safe_delete_rejects_outside_data_root(tmp_path):
    target = tmp_path / "outside.pdf"
    target.write_bytes(b"%PDF")
    with pytest.raises(SafeDeleteError):
        safe_delete_duplicate_artifact(target, data_root=tmp_path / "data", confirmed_duplicate=True)
    assert target.exists()


def test_safe_delete_removes_duplicate_inside_data_root(tmp_path):
    target = tmp_path / "data" / "raw" / "dup.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"%PDF")
    result = safe_delete_duplicate_artifact(target, data_root=tmp_path / "data", confirmed_duplicate=True)
    assert result["deleted"] is True
    assert not target.exists()


def test_validate_catalog_citations_accepts_complete_v2_entry():
    data = {"papers": [{
        "paper_id": "2024_wang_测试论文",
        "metadata": {
            "citation_key": "wang2024",
            "title": {"original": "测试论文", "translated_zh": "", "short_zh": ""},
            "authors": [{"full_name": "Wang", "family": "Wang", "given": ""}],
            "year": 2024,
            "container": {"journal": "Test Journal"},
            "identifiers": {"doi": "10.1000/test"},
        },
    }]}
    assert validate_catalog_citations(data) == []


def test_validate_catalog_citations_flags_missing_fields():
    data = {"papers": [{
        "paper_id": "2024_wang_测试论文",
        "metadata": {
            "citation_key": None,
            "title": {"original": "", "translated_zh": "", "short_zh": ""},
            "authors": [],
            "year": None,
        },
    }]}
    errors = validate_catalog_citations(data)
    assert errors  # missing author/year/title should be flagged
