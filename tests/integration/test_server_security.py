"""测试 server 安全边界：路径穿越、job_id 校验、上传格式

不依赖真实 MinerU / GPU，纯 API 边界测试。
"""
import pytest
from fastapi.testclient import TestClient

# server module 的 import 有副作用（创建目录），在 conftest 已加 sys.path
from src.server import app

client = TestClient(app)


# ---- paper_id 路径穿越防护 ----

@pytest.mark.parametrize("bad_id", [
    "../etc/passwd",
    "..\\..\\windows\\system32",
    "a/b/c",
])
def test_delete_rejects_path_traversal(bad_id):
    """DELETE /papers/{paper_id} 拒绝路径穿越"""
    resp = client.delete(f"/papers/{bad_id}")
    # 400 = validate_paper_id 拒绝；404/405 = FastAPI 路由层先拦截
    assert resp.status_code in (400, 404, 405), \
        f"Unexpected {resp.status_code}: {resp.text}"


def test_delete_valid_but_nonexistent():
    """DELETE 合法但不存在 paper_id → 404"""
    resp = client.delete("/papers/nonexistent_paper_99999")
    assert resp.status_code == 404


# ---- job_id 路径穿越防护 ----

@pytest.mark.parametrize("bad_job,path_suffix", [
    ("../../../etc", ""),
    ("001_test/../../malicious", "/files"),
    ("..\\..\\secret", "/match-catalog"),
])
def test_writer_endpoints_reject_bad_job_id(bad_job, path_suffix):
    """writer 端点拒绝含路径穿越的 job_id。
    含 / 的路径可能被 FastAPI 路由层先拦截 (404/405)，
    不含 / 但有 .. 的应被 _check_job_id 拦截 (400)。
    两种都是安全结果。
    """
    resp = client.get(f"/write/jobs/{bad_job}{path_suffix}")
    assert resp.status_code in (400, 404, 405), \
        f"Unexpected {resp.status_code}: {resp.text}"


def test_writer_endpoints_404_for_nonexistent_job():
    """合法 job_id 但不存在 → 404"""
    resp = client.get("/write/jobs/999_nonexistent_abcdef")
    assert resp.status_code == 404


# ---- 图片读取路径安全 ----

@pytest.mark.parametrize("bad_img", [
    "../../../etc/passwd.png",
    "a/b/c.jpg",
    "shell_injection;rm -rf /.png",
])
def test_images_rejects_bad_names(bad_img):
    """图片名白名单拒绝路径穿越"""
    resp = client.get(f"/papers/test_paper/images/{bad_img}")
    assert resp.status_code in (400, 404)


# ---- 上传接口 ----

def test_upload_rejects_unsupported_format():
    """不支持的文件格式 → 400"""
    resp = client.post("/upload", files={"file": ("test.exe", b"malware", "application/octet-stream")})
    assert resp.status_code == 400


def test_upload_rejects_path_traversal_filename():
    """文件名含路径穿越 → 400"""
    resp = client.post("/upload", files={
        "file": ("../../../etc/passwd.pdf", b"%PDF-1.4\nfake", "application/pdf")
    })
    assert resp.status_code == 400


# ---- 状态端点 ----

def test_status_ok():
    resp = client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "running"
    assert "version" in data
    assert "mineru_backend" in data


def test_status_runtime_ok(monkeypatch):
    from src.mineru_runtime import MinerURuntimeHealth

    monkeypatch.setattr(
        "src.mineru_runtime.preflight_gpu",
        lambda: MinerURuntimeHealth(ok=True, runner="cli", message="gpu ok", nvidia_smi=True),
    )
    monkeypatch.setattr(
        "src.mineru_runtime.preflight_torch_cuda",
        lambda: type("TorchHealth", (), {"ok": True, "message": "torch ok"})(),
    )
    monkeypatch.setattr(
        "src.mineru_runtime.preflight_mineru_cli",
        lambda exe: MinerURuntimeHealth(ok=True, runner="cli", message="cli ok", cli_available=True),
    )
    monkeypatch.setattr(
        "src.mineru_runtime.preflight_mineru_api",
        lambda url: MinerURuntimeHealth(ok=True, runner="api", message="api ok", api_available=True),
    )

    resp = client.get("/status/runtime")

    assert resp.status_code == 200
    data = resp.json()
    assert data["runtime"]["backend"] == "hybrid-engine"
    assert "gpu" in data
    assert "torch_cuda" in data
    assert "cli" in data
    assert "api" in data


# ---- 目录端点 ----

def test_catalog_endpoints_fail_closed_without_generation():
    resp = client.get("/catalog")
    assert resp.status_code == 503

    resp2 = client.post("/catalog/validate")
    assert resp2.status_code == 503


# ---- Prompt 端点边界 ----

def test_plan_reading_empty_question():
    resp = client.post("/prompt/plan-reading", json={"question": ""})
    assert resp.status_code == 422


def test_read_fulltext_empty_paper_ids():
    resp = client.post("/prompt/read-fulltext", json={
        "question": "test", "paper_ids": []
    })
    assert resp.status_code == 400


# ---- Writer job input_file 路径穿越防护 ----

@pytest.mark.parametrize("bad_input_file", [
    "/etc/passwd",
    "/etc/shadow",
    "../../.ssh/id_rsa",
    "..\\..\\windows\\system32\\config\\sam",
])
def test_create_job_rejects_arbitrary_input_file(bad_input_file):
    """create job 拒绝所有 input_file（HTTP API 不接受本地文件路径）"""
    resp = client.post("/write/jobs", json={
        "topic": "test topic",
        "input_file": bad_input_file,
    })
    assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"
    assert "input_file" in resp.text.lower() or "input_file" in resp.text, \
        f"Error message should mention input_file restriction: {resp.text}"


def test_create_write_job_lands_under_write_jobs(monkeypatch, tmp_path):
    """POST /write/jobs 必须落到 write/jobs/<job_id>/，而非旧 write/<job_id>/。"""
    import src.server as server
    from src.writer.job_manager import JobManager

    write_root = tmp_path / "write" / "jobs"
    monkeypatch.setattr(server, "job_manager", JobManager(write_dir=write_root))

    resp = client.post("/write/jobs", json={"topic": "api 路径测试"})
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    job_id = resp.json()["job_id"]

    # 新路径存在
    assert (write_root / job_id).exists()
    # 旧路径 write/<job_id>/ 不被创建
    assert not (tmp_path / "write" / job_id).exists()
