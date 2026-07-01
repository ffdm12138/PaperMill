"""paper_id 命名工具

优先级：
  1. 若 raw stem 在 DUPLICATE_RAW_STEMS 中，按已知重复处理（调用方决定跳过/标记）
  2. 若 raw stem 在 RAW_STEM_TO_PAPER_ID 中，使用规范 paper_id
  3. 否则 fallback 到清洗后的 stem

paper_id 必须通过 validate_paper_id()，防止路径穿越。
"""
import re
from pathlib import Path

try:
    from config.paper_ids import RAW_STEM_TO_PAPER_ID, DUPLICATE_RAW_STEMS
except Exception:  # pragma: no cover - 配置缺失时降级
    RAW_STEM_TO_PAPER_ID = {}
    DUPLICATE_RAW_STEMS = set()

# Windows 文件系统非法字符 + paper_id 不允许的字符
# 允许: A-Z a-z 0-9 _ - 和中文 (一-鿿)
_ILLEGAL = re.compile(r"[\\/:*?\"<>|.\s()+\-&#%!;=@~`\[\]{}',‐–—]+")
# 合法 paper_id：字母数字下划线短横线中文
_PAPER_ID_RE = re.compile(r"^[A-Za-z0-9_\-一-鿿]+$")


def safe_child(base: Path, *parts: str) -> Path:
    """安全拼接路径，防穿越。若结果不在 base 内抛 ValueError。"""
    p = base.joinpath(*parts).resolve()
    try:
        p.relative_to(base.resolve())
    except ValueError:
        raise ValueError(f"路径穿越: {p} 不在 {base} 内")
    return p


def sanitize_paper_id(raw: str) -> str:
    """清洗字符串为文件系统安全的 paper_id（保留中文，非法字符→_，空白折叠）"""
    s = _ILLEGAL.sub("_", raw)
    s = re.sub(r"\s+", "_", s.strip())
    s = s.strip("_")
    return s or "untitled"


def derive_paper_id(filename: str) -> str:
    """从文件名推导 paper_id：优先用 canonical 映射，否则清洗 stem"""
    stem = Path(filename).stem
    if stem in RAW_STEM_TO_PAPER_ID:
        return sanitize_paper_id(RAW_STEM_TO_PAPER_ID[stem])
    return sanitize_paper_id(stem)


def is_known_duplicate(filename: str) -> bool:
    """该文件是否为已知重复上传（DUPLICATE_RAW_STEMS）"""
    stem = Path(filename).stem
    return stem in DUPLICATE_RAW_STEMS


def canonical_paper_id_for(filename: str) -> str | None:
    """返回 canonical 映射的 paper_id（无映射返回 None）"""
    stem = Path(filename).stem
    if stem in RAW_STEM_TO_PAPER_ID:
        return sanitize_paper_id(RAW_STEM_TO_PAPER_ID[stem])
    return None


def validate_paper_id(paper_id: str) -> str:
    """校验 paper_id 合法性，防路径穿越。非法抛 ValueError。"""
    if not isinstance(paper_id, str) or not paper_id:
        raise ValueError(f"Invalid paper_id: {paper_id!r}")
    if not _PAPER_ID_RE.match(paper_id):
        raise ValueError(f"Invalid paper_id (含非法字符): {paper_id!r}")
    # 二次防护：禁止分隔符与穿越
    if ".." in paper_id or "/" in paper_id or "\\" in paper_id:
        raise ValueError(f"Invalid paper_id (路径穿越): {paper_id!r}")
    return paper_id


def validate_job_id(job_id: str) -> str:
    """校验 job_id 合法性，防路径穿越。非法抛 ValueError。

    job_id 格式：NNN_slug_suffix，其中 slug 来自研究主题（可含中文），
    suffix 为 6 位 hex。安全规则与 paper_id 一致。
    """
    if not isinstance(job_id, str) or not job_id:
        raise ValueError(f"Invalid job_id: {job_id!r}")
    if not _PAPER_ID_RE.match(job_id):
        raise ValueError(f"Invalid job_id (含非法字符): {job_id!r}")
    if ".." in job_id or "/" in job_id or "\\" in job_id:
        raise ValueError(f"Invalid job_id (路径穿越): {job_id!r}")
    return job_id


# 安全图片文件名：仅字母数字 . _ - + 加已知图片后缀
_SAFE_IMAGE_RE = re.compile(r'^[A-Za-z0-9_\.\-\+]+\.(png|jpg|jpeg|webp|gif|bmp)$')


def validate_image_name(img_name: str) -> str:
    """校验图片文件名合法性（白名单），防路径穿越与注入。非法抛 ValueError。"""
    if not isinstance(img_name, str) or not img_name:
        raise ValueError(f"Invalid image name: {img_name!r}")
    if not _SAFE_IMAGE_RE.match(img_name):
        raise ValueError(f"Invalid image name (含非法字符或不支持的格式): {img_name!r}")
    # 二次防护
    if ".." in img_name or "/" in img_name or "\\" in img_name:
        raise ValueError(f"Invalid image name (路径穿越): {img_name!r}")
    return img_name
