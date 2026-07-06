"""测试 JobManager 多实例隔离（WRITE_DIR 清理）"""
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from config.settings import PROJECT_ROOT
from src.writer.job_manager import WRITE_DIR, JobManager


def test_write_dir_default_is_write_jobs():
    """JobManager 默认根目录必须是 write/jobs/（writing v0.1 主路径）。"""
    assert WRITE_DIR == PROJECT_ROOT / "write" / "jobs"
    jm = JobManager()
    # job_dir 落在 write/jobs/ 下
    jdir = jm.job_dir("001_demo_abcdef")
    assert jdir.parent == WRITE_DIR
    assert jdir.parent.name == "jobs"


def test_job_manager_uses_instance_write_dir():
    """JobManager(write_dir=tmp) 的 meta 中 job_dir 应指向 tmp"""
    with tempfile.TemporaryDirectory() as td:
        jm = JobManager(write_dir=Path(td))
        info = jm.create(topic="测试任务")
        assert "job_id" in info
        meta = info["meta"]
        # job_dir 应在 tmp 内
        assert td in meta["job_dir"], \
            f"Expected job_dir under {td}, got {meta['job_dir']}"
        # jm.job_dir 也指向 tmp
        jdir = jm.job_dir(info["job_id"])
        assert str(jdir).startswith(td)


def test_two_instances_independent():
    """两个 JobManager 实例的编号不共享"""
    with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
        jm1 = JobManager(write_dir=Path(td1))
        jm2 = JobManager(write_dir=Path(td2))
        info1 = jm1.create(topic="任务A")
        info2 = jm2.create(topic="任务B")
        # 两个实例独立编号，job_dir 各自在对应 tmp 下
        assert td1 in info1["meta"]["job_dir"]
        assert td2 in info2["meta"]["job_dir"]
        assert td1 not in info2["meta"]["job_dir"]


def test_create_input_file_rejected_if_abs_path():
    """JobManager.create 拒绝绝对路径 input_file（allow_input_file=True 时）"""
    with tempfile.TemporaryDirectory() as td:
        jm = JobManager(write_dir=Path(td))
        with __import__("pytest").raises(ValueError, match="路径分隔符|路径穿越|不允许"):
            jm.create(topic="test", input_file="/etc/passwd",
                      allow_input_file=True, input_base_dir=Path(td) / "_inputs")


def test_create_input_file_rejected_if_dotdot():
    """JobManager.create 拒绝含 .. 的 input_file（allow_input_file=True 时）"""
    with tempfile.TemporaryDirectory() as td:
        jm = JobManager(write_dir=Path(td))
        with __import__("pytest").raises(ValueError, match="路径分隔符|路径穿越|不允许"):
            jm.create(topic="test", input_file="../../secret",
                      allow_input_file=True, input_base_dir=Path(td) / "_inputs")


def test_create_input_file_default_rejects():
    """JobManager.create 默认 allow_input_file=False，传 input_file 直接 ValueError"""
    with tempfile.TemporaryDirectory() as td:
        jm = JobManager(write_dir=Path(td))
        with __import__("pytest").raises(ValueError, match="默认不接受|allow_input_file"):
            jm.create(topic="test", input_file="input.md")


def test_concurrent_create_unique_jobs():
    with tempfile.TemporaryDirectory() as td:
        jm = JobManager(write_dir=Path(td))
        with ThreadPoolExecutor(max_workers=8) as pool:
            infos = list(pool.map(lambda i: jm.create(topic=f"任务{i}"), range(20)))
        ids = [info["job_id"] for info in infos]
        assert len(ids) == len(set(ids))
        for info in infos:
            jdir = Path(info["job_dir"])
            assert jdir.exists()
            assert (jdir / "logs" / "run_meta.json").exists()
