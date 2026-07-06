"""Pure v2 project configuration."""
import os
import warnings
from pathlib import Path


def _env_str(name: str, default: str) -> str:
    """读取字符串环境变量，空字符串视为未设置"""
    val = os.environ.get(name, "").strip()
    return val if val else default


def _env_int(name: str, default: int, min_val: int | None = None,
             max_val: int | None = None) -> int:
    """读取整数环境变量，非法值时 warning 后回退默认值"""
    val = os.environ.get(name, "").strip()
    if not val:
        return default
    try:
        v = int(val)
    except ValueError:
        warnings.warn(f"{name}={val!r} 非法整数，回退默认 {default}")
        return default
    if min_val is not None and v < min_val:
        warnings.warn(f"{name}={v} < {min_val}，回退默认 {default}")
        return default
    if max_val is not None and v > max_val:
        warnings.warn(f"{name}={v} > {max_val}，回退默认 {default}")
        return default
    return v


def _env_bool(name: str, default: bool = False) -> bool:
    """读取布尔环境变量。"""
    val = os.environ.get(name, "").strip().lower()
    if not val:
        return default
    return val in {"1", "true", "yes", "on"}


def _env_path(name: str, default: Path) -> Path:
    """读取路径环境变量"""
    val = os.environ.get(name, "").strip()
    return Path(val) if val else Path(default)

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent

# 数据目录
DATA_DIR = _env_path("MINERU_DATA_DIR", PROJECT_ROOT / "data")
RAW_DIR = DATA_DIR / "raw"
PAPER_RAW_DIR = DATA_DIR / "paper_raw"
PAPERS_DIR = DATA_DIR / "papers"
MINERU_TMP_DIR = DATA_DIR / "tmp" / "mineru_raw_output"  # MinerU 原始输出临时目录，处理完可清空
MINERU_LOG_DIR = DATA_DIR / "logs"          # MinerU 转换性能日志

# 目录与账本
CATALOG_DIR = DATA_DIR / "catalog"          # AI 维护的文献理解目录
ALL_CATALOG_PATH = CATALOG_DIR / "all.catalog.json"
PAPER_NUMBER_LEDGER_PATH = CATALOG_DIR / "paper_number_ledger.json"
DISCOVERY_DIR = DATA_DIR / "discovery"
JOBS_DIR = DATA_DIR / "jobs"
UPLOAD_JOBS_PATH = JOBS_DIR / "upload_jobs.json"
UPLOAD_STAGING_DIR = JOBS_DIR / "upload_staging"

# CUDA 路径（MinerU lmdeploy 后端需要，默认 Windows 标准路径）
CUDA_PATH_DEFAULT = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.6"
CUDA_PATH = _env_str("CUDA_PATH", CUDA_PATH_DEFAULT)
# 自动注入进程环境变量，确保子进程（MinerU lmdeploy）能继承
os.environ.setdefault("CUDA_PATH", CUDA_PATH)

# 代理配置（默认空=直连）
FETCH_PROXY = _env_str("FETCH_PROXY", "")

# Publisher TDM API 密钥（免费注册后使用，默认空=下降级通道）
# Wiley: https://onlinelibrary.wiley.com/tdm  →  WILEY_TDM_TOKEN
# Elsevier: https://dev.elsevier.com  →  ELSEVIER_API_KEY
# Springer: 无需密钥，直接构造 PDF URL
WILEY_TDM_TOKEN = _env_str("WILEY_TDM_TOKEN", "")
ELSEVIER_API_KEY = _env_str("ELSEVIER_API_KEY", "")

# API 配置（默认仅 localhost，防误暴露）
API_HOST = _env_str("MINERU_API_HOST", "127.0.0.1")
API_PORT = _env_int("MINERU_API_PORT", 8080, min_val=1, max_val=65535)
MINERU_API_KEY = _env_str("MINERU_API_KEY", "")
MINERU_API_CORS_ORIGINS = _env_str(
    "MINERU_API_CORS_ORIGINS",
    "http://127.0.0.1:8080,http://localhost:8080",
)
API_CORS_ORIGINS = [
    origin.strip()
    for origin in MINERU_API_CORS_ORIGINS.split(",")
    if origin.strip()
]
MINERU_ALLOW_UNAUTHENTICATED_PUBLIC_API = _env_bool(
    "MINERU_ALLOW_UNAUTHENTICATED_PUBLIC_API",
    False,
)
# 上传大小上限（字节），默认 500MB
MAX_UPLOAD_SIZE = _env_int("MINERU_MAX_UPLOAD_SIZE", 500 * 1024 * 1024, min_val=1)
# PDF fetch 下载大小上限（字节），默认 200MB
MINERU_FETCH_MAX_BYTES = _env_int("MINERU_FETCH_MAX_BYTES", 200 * 1024 * 1024, min_val=1)

# MinerU 最大并行转换数（默认 1，防 OOM。多 GPU 时可适当调大）
MINERU_MAX_WORKERS = _env_int("MINERU_MAX_WORKERS", 1, min_val=1)
# 成功转换后是否保留 MinerU 临时输出；失败始终保留用于排查
MINERU_KEEP_TMP = _env_bool("MINERU_KEEP_TMP", False)
# 本地 MinerU 原始输出缓存/历史，用于同一 PDF 的可验证恢复；不入 git/snapshot。
MINERU_OUTPUT_CACHE_ENABLED = _env_bool("MINERU_OUTPUT_CACHE_ENABLED", True)
MINERU_OUTPUT_CACHE_DIR = _env_path("MINERU_OUTPUT_CACHE_DIR", PROJECT_ROOT / "output" / "mineru_cache")

# MinerU 解析配置
# 产品定位固定为 hybrid-engine，不再把 pipeline / vlm-engine 作为首选项维护。
# 高级调试时可设环境变量 MINERU_ALLOW_BACKEND_OVERRIDE=true 恢复多后端选项。
ALLOW_BACKEND_OVERRIDE = os.environ.get("MINERU_ALLOW_BACKEND_OVERRIDE", "").strip().lower() == "true"
MINERU_BACKEND = "hybrid-engine"
MINERU_EFFORT = "medium"
MINERU_METHOD = "auto"
MINERU_LANG = _env_str("MINERU_LANG", "ch")

# 高级调试：仅 ALLOW_BACKEND_OVERRIDE=true 时允许覆盖
if ALLOW_BACKEND_OVERRIDE:
    MINERU_BACKEND = _env_str("MINERU_BACKEND", "hybrid-engine")
    MINERU_EFFORT = _env_str("MINERU_EFFORT", "medium")
    MINERU_METHOD = _env_str("MINERU_METHOD", "auto")

# 合法值校验（仅当覆盖开启时才放宽）
VALID_BACKENDS = {"hybrid-engine", "pipeline", "vlm-engine"} if ALLOW_BACKEND_OVERRIDE else {"hybrid-engine"}
VALID_EFFORTS = {"medium", "high"} if ALLOW_BACKEND_OVERRIDE else {"medium"}
VALID_METHODS = {"auto", "txt", "ocr"}
if MINERU_BACKEND not in VALID_BACKENDS:
    raise ValueError(f"非法 MINERU_BACKEND: {MINERU_BACKEND}，允许: {VALID_BACKENDS}")
if MINERU_EFFORT not in VALID_EFFORTS:
    raise ValueError(f"非法 MINERU_EFFORT: {MINERU_EFFORT}，允许: {VALID_EFFORTS}")
if MINERU_METHOD not in VALID_METHODS:
    raise ValueError(f"非法 MINERU_METHOD: {MINERU_METHOD}，允许: {VALID_METHODS}")


def enforce_backend_effort_override(parser, args) -> None:
    """CLI 共享校验：普通模式（ALLOW_BACKEND_OVERRIDE=false）禁止覆盖 backend/effort。

    产品固定 hybrid-engine + medium。仅当环境变量 MINERU_ALLOW_BACKEND_OVERRIDE=true
    时才允许 pipeline/vlm-engine/high。违反时 parser.error 退出。
    method 不受限（auto/ocr/txt 均可）。
    """
    if ALLOW_BACKEND_OVERRIDE:
        return
    if getattr(args, "backend", None) and args.backend != MINERU_BACKEND:
        parser.error(
            f"--backend={args.backend} 不被允许：产品固定 {MINERU_BACKEND}。"
            f"如需高级调试（pipeline/vlm-engine），设置环境变量 "
            f"MINERU_ALLOW_BACKEND_OVERRIDE=true。")
    if getattr(args, "effort", None) and args.effort != MINERU_EFFORT:
        parser.error(
            f"--effort={args.effort} 不被允许：产品固定 {MINERU_EFFORT}。"
            f"如需高级调试（high），设置环境变量 "
            f"MINERU_ALLOW_BACKEND_OVERRIDE=true。")

# 全文阅读 prompt 的单篇最大字符数（防止 prompt 过长）
PAPER_MD_MAX_CHARS = _env_int("MINERU_PAPER_MD_MAX_CHARS", 12000, min_val=1)

# 文献研究方向配置（用于 catalog screening.relevance_score 与 research_card 字段）
RESEARCH_DOMAIN = _env_str("MINERU_RESEARCH_DOMAIN", "")

# 写作/综述风格配置（用于 prompt builder）
RESEARCH_QUESTION = _env_str("MINERU_RESEARCH_QUESTION", "")
WRITING_STYLE = _env_str("MINERU_WRITING_STYLE", "technical Chinese academic writing")
CITATION_STYLE = _env_str("MINERU_CITATION_STYLE", "author-year")

# 支持的文件格式 (MinerU 3.4)
SUPPORTED_FORMATS = {".pdf", ".docx", ".pptx", ".xlsx", ".png", ".jpg", ".jpeg"}

# 确保目录存在（导入即创建，有副作用）
for d in [
    RAW_DIR, PAPER_RAW_DIR, PAPERS_DIR,
    MINERU_TMP_DIR, MINERU_LOG_DIR,
    CATALOG_DIR, DISCOVERY_DIR,
    JOBS_DIR, UPLOAD_STAGING_DIR,
]:
    d.mkdir(parents=True, exist_ok=True)

# 启动时安全检查：若 API_HOST 非 localhost，必须显式配置 API key 或 unsafe override。
if API_HOST not in ("127.0.0.1", "localhost", "::1"):
    if not MINERU_API_KEY and not MINERU_ALLOW_UNAUTHENTICATED_PUBLIC_API:
        raise RuntimeError(
            f"API_HOST={API_HOST} is not localhost. Set MINERU_API_KEY before exposing "
            "the API, or explicitly set MINERU_ALLOW_UNAUTHENTICATED_PUBLIC_API=true "
            "for an unsafe local-only override."
        )
    if MINERU_ALLOW_UNAUTHENTICATED_PUBLIC_API and not MINERU_API_KEY:
        warnings.warn(
            f"API_HOST={API_HOST} is not localhost and MINERU_API_KEY is empty. "
            "MINERU_ALLOW_UNAUTHENTICATED_PUBLIC_API=true is unsafe; use only behind "
            "trusted network controls.",
            RuntimeWarning)
