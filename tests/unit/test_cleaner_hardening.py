from src.cleaner import MinerUOutputCleaner


def test_cleaner_uses_injected_papers_dir(tmp_path):
    source = tmp_path / "mineru" / "doc" / "hybrid_auto"
    source.mkdir(parents=True)
    (source / "doc.md").write_text("# ok", encoding="utf-8")

    papers_dir = tmp_path / "papers"
    cleaner = MinerUOutputCleaner(papers_dir=papers_dir)
    result = cleaner.extract(tmp_path / "mineru" / "doc", "2024_wang_测试论文",
                             method="auto", stem="doc", backend="hybrid-engine")

    assert result["success"]
    assert (papers_dir / "2024_wang_测试论文" / "2024_wang_测试论文.md").exists()


def test_cleaner_copies_only_images_from_unique_fallback_dir(tmp_path):
    source = tmp_path / "mineru" / "doc" / "hybrid_auto"
    images = tmp_path / "mineru" / "doc" / "assets" / "images"
    source.mkdir(parents=True)
    images.mkdir(parents=True)
    (source / "doc.md").write_text("![](images/a.png)", encoding="utf-8")
    (images / "a.png").write_bytes(b"img")
    (images / "note.txt").write_text("no", encoding="utf-8")

    cleaner = MinerUOutputCleaner(papers_dir=tmp_path / "papers")
    result = cleaner.extract(tmp_path / "mineru" / "doc", "2024_wang_图片论文",
                             method="auto", stem="doc", backend="hybrid-engine")

    dest_images = tmp_path / "papers" / "2024_wang_图片论文" / "images"
    assert result["images_count"] == 1
    assert (dest_images / "a.png").exists()
    assert not (dest_images / "note.txt").exists()


def test_cleaner_skips_ambiguous_recursive_images_dirs(tmp_path):
    source = tmp_path / "mineru" / "doc" / "hybrid_auto"
    source.mkdir(parents=True)
    (source / "doc.md").write_text("# ok", encoding="utf-8")
    (tmp_path / "mineru" / "doc" / "a" / "images").mkdir(parents=True)
    (tmp_path / "mineru" / "doc" / "b" / "images").mkdir(parents=True)

    cleaner = MinerUOutputCleaner(papers_dir=tmp_path / "papers")
    result = cleaner.extract(tmp_path / "mineru" / "doc", "2024_wang_多图目录",
                             method="auto", stem="doc", backend="hybrid-engine")

    assert result["success"]
    assert result["images_count"] == 0
