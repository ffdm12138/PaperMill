"""app.list_papers reads catalog v3 content titles without metadata."""

import app


class FakeCatalog:
    def __init__(self, papers):
        self._papers = papers

    def list_papers(self):
        return list(self._papers)


def test_list_papers_reads_content_title_zh(monkeypatch):
    monkeypatch.setattr(app, "catalog", FakeCatalog([{
        "paper_number": "0000000000000001",
        "paper_id": "2024_wang_测试",
        "content_identity": {"content_title_zh": "内容标题示例"},
    }]))
    out = app.list_papers()
    assert "内容标题示例" in out
    assert "0000000000000001" in out
    assert "2024_wang_测试" in out


def test_list_papers_does_not_assume_metadata_field(monkeypatch):
    monkeypatch.setattr(app, "catalog", FakeCatalog([{
        "paper_number": "0000000000000002",
        "paper_id": "2024_li_另一篇",
        "content_identity": {"content_title_zh": "另一篇标题"},
    }]))
    out = app.list_papers()
    assert "另一篇标题" in out


def test_list_papers_empty_catalog(monkeypatch):
    monkeypatch.setattr(app, "catalog", FakeCatalog([]))
    out = app.list_papers()
    assert "为空" in out
