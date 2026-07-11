import pytest

from src.utils.safe_delete import SafeDeleteError, safe_delete_duplicate_artifact


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


def test_safe_delete_removes_confirmed_duplicate_inside_data_root(tmp_path):
    target = tmp_path / "data" / "raw" / "dup.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"%PDF")
    result = safe_delete_duplicate_artifact(target, data_root=tmp_path / "data", confirmed_duplicate=True)
    assert result["deleted"] is True and not target.exists()


def test_safe_delete_refuses_directory_symlink_without_touching_target(tmp_path):
    root = tmp_path / "data"
    valuable = root / "valuable"
    valuable.mkdir(parents=True)
    sentinel = valuable / "KEEP.txt"
    sentinel.write_text("keep", encoding="utf-8")
    link = root / "link"
    try:
        link.symlink_to(valuable, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(SafeDeleteError, match="symlink"):
        safe_delete_duplicate_artifact(link, data_root=root, confirmed_duplicate=True)
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert link.is_symlink()
