"""写作任务管理：创建/列出/加载 write/jobs/<job>/ 目录结构

任务目录命名：write/jobs/001_<topic_slug>_<suffix>/，suffix 防并发碰撞。

"""
import json
import os
import re
import shutil
from pathlib import Path
from datetime import datetime
from loguru import logger

from config.settings import PROJECT_ROOT
from filelock import FileLock
from src.utils.atomic_io import atomic_write_json

WRITE_DIR = PROJECT_ROOT / "write" / "jobs"

# 任务标准子目录
JOB_SUBDIRS = ["input", "planning", "reading/paper_notes", "tex/sections",
               "figures", "logs/prompts"]


def _slugify(topic: str) -> str:
    """从研究内容生成 slug：取前 6-16 个中文字符或 3-8 个英文词，清洗非法字符"""
    topic = topic.strip()
    if not topic:
        return "untitled"
    # 优先取中文连续段
    zh = re.findall(r"[一-鿿]+", topic)
    if zh:
        s = "".join(zh)[:16]
    else:
        words = re.findall(r"[A-Za-z0-9]+", topic)[:8]
        s = "_".join(words) if words else "untitled"
    # 文件系统安全
    s = re.sub(r'[\\/:*?"<>|]', "", s)
    return s.strip() or "untitled"


def _next_job_num(write_dir: Path = WRITE_DIR) -> int:
    """扫描 write_dir/ 找最大编号 +1"""
    if not write_dir.exists():
        return 1
    max_n = 0
    for d in write_dir.iterdir():
        m = re.match(r"^(\d+)_", d.name)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n + 1


def _job_id_suffix() -> str:
    """生成微秒 hex 后缀（6 字符），防并发碰撞"""
    from datetime import datetime
    return format(datetime.now().microsecond, '06x')


def _empty_run_meta(job_id: str, job_dir: Path, topic: str, input_type: str,
                    target: str, language: str) -> dict:
    return {
        "job_id": job_id,
        "job_dir": str(job_dir),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "input_type": input_type,
        "target": target,
        "language": language,
        "topic": topic,
        "status": "created",
        "steps": {
            "catalog_match_prompt_generated": False,
            "catalog_selection_confirmed": False,
            "deep_read_prompt_generated": False,
            "deep_read_notes_filled": False,
            "story_prompt_generated": False,
            "story_plan_filled": False,
            "tex_template_generated": False,
            "tex_content_filled": False,
            "figures_copied": False,
            "bib_exported": False,
            "validated": False,
        },
        "selected_papers": [],
        "used_bib_keys": [],
        "used_figures": [],
        "notes": [],
    }


class JobManager:
    """写作任务目录管理"""

    def __init__(self, write_dir: Path = WRITE_DIR):
        self.write_dir = Path(write_dir)

    def job_dir(self, job_id: str) -> Path:
        from src.naming import validate_job_id, safe_child
        validate_job_id(job_id)  # 防路径穿越
        return safe_child(self.write_dir, job_id)

    def list_jobs(self) -> list[dict]:
        """列出所有任务的 run_meta 摘要"""
        out = []
        if not self.write_dir.exists():
            return out
        for d in sorted(self.write_dir.iterdir()):
            if d.is_dir() and re.match(r"^\d+_", d.name):
                meta_path = d / "logs" / "run_meta.json"
                if meta_path.exists():
                    out.append(json.loads(meta_path.read_text(encoding="utf-8")))
                else:
                    out.append({"job_id": d.name, "status": "created"})
        return out

    def load_meta(self, job_id: str) -> dict | None:
        p = self.job_dir(job_id) / "logs" / "run_meta.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def _meta_lock_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / ".meta.lock"

    def _jobs_lock_path(self) -> Path:
        return self.write_dir / ".jobs.lock"

    def _locked_meta(self, job_id: str, fn) -> dict:
        """事务级锁：锁住 load_meta → 修改 → save_meta 完整周期"""
        lock = FileLock(str(self._meta_lock_path(job_id)))
        with lock:
            meta = self.load_meta(job_id)
            if meta is None:
                raise FileNotFoundError(f"任务不存在: {job_id}")
            result = fn(meta)
            self.save_meta(job_id, meta)
            return result

    def save_meta(self, job_id: str, meta: dict) -> None:
        """原子写入 run_meta.json：tmp + os.replace，避免中断/并发损坏"""
        meta["updated_at"] = datetime.now().isoformat(timespec="seconds")
        p = self.job_dir(job_id) / "logs" / "run_meta.json"
        atomic_write_json(p, meta, indent=2)

    def touch(self, job_id: str) -> None:
        """仅刷新 updated_at"""
        try:
            self._locked_meta(job_id, lambda m: None)
        except FileNotFoundError:
            pass

    def set_step(self, job_id: str, step: str, value: bool = True,
                 extra: dict | None = None) -> dict:
        def _fn(meta):
            if step not in meta["steps"]:
                raise KeyError(f"未知 step: {step}")
            meta["steps"][step] = value
            if extra:
                meta.update(extra)
            return meta
        return self._locked_meta(job_id, _fn)

    def set_status(self, job_id: str, status: str) -> dict:
        return self._locked_meta(job_id, lambda m: m.update(status=status) or m)

    def set_selected_papers(self, job_id: str, paper_names: list[str]) -> dict:
        return self._locked_meta(job_id, lambda m: m.update(selected_papers=list(paper_names)) or m)

    def append_note(self, job_id: str, note: str) -> dict:
        def _fn(meta):
            meta.setdefault("notes", []).append(
                f"[{datetime.now().isoformat(timespec='seconds')}] {note}")
            return meta
        return self._locked_meta(job_id, _fn)

    def step_is(self, job_id: str, step: str) -> bool:
        """安全查询某 step 是否为 True"""
        meta = self.load_meta(job_id)
        if meta is None:
            return False
        return bool(meta.get("steps", {}).get(step, False))

    def require_step(self, job_id: str, step: str, action: str) -> None:
        """前置校验：若 step 未完成则抛 RuntimeError，提示先做某动作"""
        if not self.step_is(job_id, step):
            raise RuntimeError(
                f"Cannot {action}: run_meta.steps.{step} is not True. "
                f"请先完成前置步骤。")

    def create(self, topic: str | None = None, input_file: str | None = None,
               target: str = "phd_thesis", language: str = "zh",
               allow_input_file: bool = False,
               input_base_dir: Path | None = None) -> dict:
        """创建一个写作任务目录。topic 与 input_file 至少给一个。

        input_file 安全策略：
        - 默认 allow_input_file=False，传 input_file 直接 ValueError
        - CLI 可显式 allow_input_file=True + input_base_dir=<write_inputs/>
        - 即使 allow_input_file=True，也只用 safe_child 读取，拒绝 .. 和绝对路径
        """
        if not topic and not input_file:
            raise ValueError("必须提供 topic 或 input_file")

        # 读取输入文件内容
        input_text = ""
        if input_file:
            if not allow_input_file:
                raise ValueError(
                    "JobManager.create 默认不接受 input_file 路径。"
                    "请通过 topic 参数直接传文本，"
                    "或显式传 allow_input_file=True 并使用 write_inputs/ 目录。")
            # 只允许纯文件名（无路径分隔符）
            if os.path.isabs(input_file) or ".." in input_file \
                    or "/" in input_file or "\\" in input_file:
                raise ValueError(
                    f"input_file 不允许路径分隔符或穿越: {input_file!r}")
            base = input_base_dir or (self.write_dir / "_inputs")
            from src.naming import safe_child
            ip = safe_child(base, input_file)
            if not ip.exists():
                raise FileNotFoundError(f"输入文件不存在: {ip}")
            input_text = ip.read_text(encoding="utf-8")
            if not topic:
                topic = input_text[:60]

        slug = _slugify(topic or "untitled")
        input_type = "input_file" if input_file else "topic_text"
        self.write_dir.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self._jobs_lock_path())):
            for _ in range(20):
                num = _next_job_num(self.write_dir)
                suffix = _job_id_suffix()
                job_id = f"{num:03d}_{slug}_{suffix}"
                jdir = self.job_dir(job_id)
                try:
                    jdir.mkdir(parents=True, exist_ok=False)
                except FileExistsError:
                    continue
                try:
                    for sub in JOB_SUBDIRS:
                        (jdir / sub).mkdir(parents=True, exist_ok=True)
                    (jdir / "input" / "research_input.md").write_text(
                        input_text if input_text else f"# 研究内容\n\n{topic}\n",
                        encoding="utf-8",
                    )
                    meta = _empty_run_meta(job_id, jdir, topic or slug, input_type,
                                           target, language)
                    self.save_meta(job_id, meta)
                except Exception:
                    shutil.rmtree(jdir, ignore_errors=True)
                    raise
                break
            else:
                raise RuntimeError("failed to allocate a unique job_id after retries")

        logger.info(f"创建写作任务: {job_id} -> {jdir}")
        return {"job_id": job_id, "job_dir": str(jdir), "meta": meta}

    def job_files(self, job_id: str) -> list[str]:
        """列出任务目录下所有文件（相对 job_dir 的路径）"""
        jdir = self.job_dir(job_id)
        if not jdir.exists():
            return []
        return [str(p.relative_to(jdir)).replace("\\", "/")
                for p in jdir.rglob("*") if p.is_file()]
