"""测试 API 对非法 paper_id/job_id 统一返回 400（不冒泡成 500）"""
import pytest
from fastapi.testclient import TestClient
from src.server import app

client = TestClient(app)


@pytest.mark.parametrize("bad_id", [
    "../../evil",
    "a/b",
    "a:b",
    "..\\..\\etc",
])
def test_get_paper_rejects_bad_id(bad_id):
    resp = client.get(f"/papers/{bad_id}")
    # 400 = validate_paper_id 拒绝；404 = FastAPI 路由层拦截含 / 或 .. 的路径
    assert resp.status_code in (400, 404), \
        f"Expected 400 or 404, got {resp.status_code}: {resp.text}"


@pytest.mark.parametrize("bad_id", [
    "../../evil",
    "a/b",
    "a:b",
])
def test_get_paper_markdown_rejects_bad_id(bad_id):
    resp = client.get(f"/papers/{bad_id}/markdown")
    assert resp.status_code in (400, 404), \
        f"Expected 400 or 404, got {resp.status_code}: {resp.text}"


@pytest.mark.parametrize("bad_id", [
    "../../evil",
    "a/b",
])
def test_get_paper_images_rejects_bad_id(bad_id):
    resp = client.get(f"/papers/{bad_id}/images")
    assert resp.status_code in (400, 404), \
        f"Expected 400 or 404, got {resp.status_code}: {resp.text}"


def test_catalog_entry_rejects_bad_paper_id():
    resp = client.post("/prompt/catalog-entry", json={"paper_id": "../../evil"})
    assert resp.status_code == 400


def test_read_fulltext_rejects_bad_paper_ids():
    resp = client.post("/prompt/read-fulltext", json={
        "question": "test",
        "paper_ids": ["valid_id", "../../evil"]
    })
    assert resp.status_code == 400
