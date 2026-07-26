"""MinerU 输出清理器

定位 MinerU 原始输出中的正文 Markdown 与 images 目录（供 PaperRawConverter 使用）。
``extract()`` 是历史辅助入口，按 v2 语义把正文写成 ``<paper_name>.md``。
不复制 PDF，不保留 json sidecars。
"""
import shutil
from pathlib import Path
from loguru import logger

from config.settings import PAPERS_DIR

# MinerU 中间产物后缀（均为 .json，非正文 .md）：_model/_middle/_content_list/_origin
# 正文 md 文件名等于 stem，可能含 model 等词，故不按 token 排除 .md。


class MinerUOutputCleaner:
    """定位 MinerU 输出正文 Markdown 与 images/，并按 v2 语义提取为 <paper_name>.md。"""

    IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

    def __init__(self, papers_dir: Path = PAPERS_DIR):
        self.papers_dir = Path(papers_dir)

    @staticmethod
    def _method_dirs(method: str, backend: str | None = None) -> list[str]:
        """返回给定 method 应搜索的目录名列表。

        产品固定 hybrid-engine，hybrid_* 目录优先。
        仅 ALLOW_BACKEND_OVERRIDE 时考虑 vlm/pipeline 原生目录。
        """
        _map = {
            "auto": ["hybrid_auto", "auto"],
            "ocr":  ["hybrid_ocr", "ocr"],
            "txt":  ["hybrid_txt", "txt"],
        }
        dirs = _map.get(method, [method])
        if backend == "vlm-engine":
            # vlm 变体仅在高级覆盖时考虑
            vlm_map = {"auto": "vlm_auto", "ocr": "vlm_ocr", "txt": "vlm_txt"}
            if method in vlm_map:
                dirs.insert(0, vlm_map[method])
        elif backend == "pipeline":
            # pipeline 原生目录优先
            if method in dirs:
                dirs.remove(method)
                dirs.insert(0, method)
        # 默认 hybrid-engine：hybrid_* 已在首位
        return dirs

    # 目录名 → (method, backend) 反向映射。用于校验 source_dir 自身是否
    # 与调用方声明的 method/backend 一致——禁止目录名反向决定语义。
    _DIRNAME_MAP = {
        "hybrid_auto": ("auto", "hybrid-engine"),
        "hybrid_txt":  ("txt",  "hybrid-engine"),
        "hybrid_ocr":  ("ocr",  "hybrid-engine"),
        "auto": ("auto", "pipeline"),
        "txt":  ("txt",  "pipeline"),
        "ocr":  ("ocr",  "pipeline"),
        "vlm_auto": ("auto", "vlm-engine"),
        "vlm_txt":  ("txt",  "vlm-engine"),
        "vlm_ocr":  ("ocr",  "vlm-engine"),
    }

    @classmethod
    def _method_from_dirname(cls, dirname: str) -> tuple[str | None, str | None]:
        """目录名 → (method, backend)；未知目录名返回 (None, None)。"""
        return cls._DIRNAME_MAP.get(dirname, (None, None))

    @staticmethod
    def _backend_compatible(detected_backend: str, backend: str | None) -> bool:
        """检测到的目录 backend 是否与声明的 backend 兼容。

        hybrid-engine 目录可被 hybrid-engine 或 pipeline 接受（产品默认）；
        vlm-engine 目录仅 vlm-engine 接受；pipeline 原生目录任何 backend 都接受。
        """
        if detected_backend == "hybrid-engine":
            return backend in ("hybrid-engine", "pipeline", None)
        if detected_backend == "vlm-engine":
            return backend in ("vlm-engine", None)
        if detected_backend == "pipeline":
            return True
        return False

    @classmethod
    def _source_dir_matches_method(cls, source_dir: Path,
                                   method: str | None,
                                   backend: str | None) -> bool:
        """source_dir 自身为 method 目录时，必须与声明的 method/backend 一致。

        若 source_dir.name 不是已知 method 目录（例如是 <stem> 目录或 tmp 根），
        返回 True（不构成约束，由内部 exact path 校验）。
        """
        detected_method, detected_backend = cls._method_from_dirname(source_dir.name)
        if detected_method is None:
            return True
        if method is not None and detected_method != method:
            return False
        if not cls._backend_compatible(detected_backend, backend):
            return False
        return True

    def locate_markdown(self, source_dir: Path,
                        method: str | None = None,
                        stem: str | None = None,
                        backend: str | None = None) -> Path | None:
        """在 MinerU 输出目录中定位正文 Markdown。

        支持两种 source_dir 传入方式：
          A. tmp_out/<stem>/   — converter 返回 output_dir/stem（最常见）
          B. tmp_out/          — 直接传 MinerU 输出根目录

        选择规则（确定性，不依赖 rglob 顺序）：
          a. exact path（若提供 method + stem）：
             按 _method_dirs(method, backend) 依次检查各目录
          b. method 是硬约束，找不到不 fallback
          c. source_dir 下唯一 .md → 取它
          d. 唯一 method 候选 → 取它（含 hybrid/vlm 变体）
          e. 多候选 → 按 backend 优先级选一个，否则返回 None
        """
        source_dir = Path(source_dir)
        if not source_dir.exists():
            return None

        # a. exact path（若提供了 method 和 stem）
        if method and stem:
            dirs = self._method_dirs(method, backend)
            exact_matches = []
            for d in dirs:
                # 模式 A: source_dir 已是 <stem>/ 目录
                exact_a = source_dir / d / f"{stem}.md"
                if exact_a.is_file():
                    exact_matches.append(exact_a)
                # 模式 B: source_dir 是 tmp_out 根目录
                exact_b = source_dir / stem / d / f"{stem}.md"
                if exact_b.is_file():
                    exact_matches.append(exact_b)
            # 扁平结构兜底：source_dir 自身就是 method 目录（如 hybrid_ocr/stem.md）
            # 必须校验该目录与声明的 method/backend 一致，禁止目录名反向决定语义
            exact_c = source_dir / f"{stem}.md"
            if exact_c.is_file() and self._source_dir_matches_method(
                    source_dir, method, backend):
                exact_matches.append(exact_c)

            if len(exact_matches) == 1:
                return exact_matches[0]
            if len(exact_matches) > 1:
                # 按 backend 优先级排序
                def _prio(md_path):
                    name = md_path.parent.name
                    if backend == "hybrid-engine" and name.startswith("hybrid_"):
                        return 0
                    if backend == "vlm-engine" and name.startswith("vlm_"):
                        return 0
                    if backend == "pipeline" and not name.startswith(("hybrid_", "vlm_")):
                        return 0
                    return 1
                exact_matches.sort(key=_prio)
                if backend and _prio(exact_matches[0]) < _prio(exact_matches[1]):
                    return exact_matches[0]
                names = ", ".join(str(m.relative_to(source_dir)) for m in exact_matches)
                logger.error(
                    f"多个 exact match 候选 md 文件，无法确定正文: {names}。"
                    f"请指定 backend 参数或清理残留输出。")
                return None
            # method 是硬约束，找不到不 fallback
            checked = [source_dir / d / f"{stem}.md" for d in dirs]
            logger.error(
                f"未找到指定 method={method} backend={backend} stem={stem} "
                f"的正文 md，已检查: {[str(c) for c in checked]}")
            return None

        candidates = list(source_dir.rglob("*.md"))
        if not candidates:
            return None

        # method 给定但 stem 未给：method 仍是硬约束。
        # 只接受 parent.name 落在 _method_dirs(method) 内的候选，
        # 禁止用目录名反向决定语义（例如 method=ocr 时 hybrid_auto 不得命中）。
        if method:
            allowed = set(self._method_dirs(method, backend))
            method_cands = [c for c in candidates if c.parent.name in allowed]
            if len(method_cands) == 1:
                return method_cands[0]
            if len(method_cands) > 1:
                def _prio(cand):
                    name = cand.parent.name
                    if backend == "hybrid-engine" and name.startswith("hybrid_"):
                        return 0
                    if backend == "vlm-engine" and name.startswith("vlm_"):
                        return 0
                    if backend == "pipeline" and not name.startswith(("hybrid_", "vlm_")):
                        return 0
                    return 1
                method_cands.sort(key=_prio)
                if _prio(method_cands[0]) < _prio(method_cands[1]):
                    return method_cands[0]
                names = ", ".join(str(c.relative_to(source_dir)) for c in method_cands)
                logger.error(
                    f"method={method} 下多个候选 md 文件，无法确定正文: {names}。"
                    f"请指定 stem/backend 或清理残留输出。")
                return None
            # method 给定但无任何匹配目录 → 硬约束失败，不 fallback 到非 method 目录
            checked = sorted(c.parent.name for c in candidates)
            logger.error(
                f"method={method} backend={backend} 下未找到匹配目录的正文 md，"
                f"现有目录: {checked}")
            return None

        if len(candidates) == 1:
            return candidates[0]

        # b. 多个候选：查找标准 method 目录下的 md
        all_method_dirs = set()
        for m in ["auto", "ocr", "txt"]:
            all_method_dirs.update(self._method_dirs(m, backend))
        method_cands = [c for c in candidates if c.parent.name in all_method_dirs]

        if len(method_cands) == 1:
            return method_cands[0]

        if len(method_cands) > 1:
            # 按 backend 优先级排序
            def _prio(cand):
                name = cand.parent.name
                if backend == "hybrid-engine" and name.startswith("hybrid_"):
                    return 0
                if backend == "vlm-engine" and name.startswith("vlm_"):
                    return 0
                if backend == "pipeline" and not name.startswith(("hybrid_", "vlm_")):
                    return 0
                return 1
            method_cands.sort(key=_prio)
            if _prio(method_cands[0]) < _prio(method_cands[1]):
                return method_cands[0]
            names = ", ".join(str(c.relative_to(source_dir)) for c in method_cands)
            logger.error(
                f"多个 method 目录候选 md 文件，无法确定正文: {names}。"
                f"请指定 method/backend 参数或清理残留输出。")
            return None

        # c. 无 method 目录候选：报错列全部候选
        names = ", ".join(str(c.relative_to(source_dir)) for c in candidates)
        logger.error(f"多个候选 md 文件且不在标准 method 目录，无法确定正文: {names}")
        return None

    def locate_images_dir(self, source_dir: Path, md_path: Path) -> Path | None:
        """定位 md 引用的 images 目录"""
        # 优先 md 同级 images/
        img_dir = md_path.parent / "images"
        if img_dir.is_dir():
            return img_dir
        # 退而递归找：只有唯一 images/ 目录时才接受，避免误复制别的 method 输出
        candidates = [d for d in source_dir.rglob("images") if d.is_dir()]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            logger.warning(f"多个 images 目录，跳过图片复制: {[str(c) for c in candidates]}")
        return None

    def extract(self, source_dir: str | Path, paper_name: str,
                overwrite: bool = False,
                method: str | None = None,
                stem: str | None = None,
                backend: str | None = None) -> dict:
        """从 MinerU 原始输出目录提取 <paper_name>.md + images 到 data/papers/<paper_name>/

        覆盖保护：默认 overwrite=False，目标已存在则报错；overwrite=True 先备份再重建。

        Args:
            method: MinerU 解析方法 (auto/ocr/txt)，用于确定性定位正文 md
            stem: 输入文件名 stem，用于 exact path 匹配
            backend: 解析后端 (pipeline/hybrid-engine/vlm-engine)，影响 hybrid/vlm 目录优先级

        Returns:
            {
                "success": bool,
                "paper_name": str,
                "markdown_path": str,
                "images_dir": str,
                "images_count": int,
                "char_count": int,
                "error": str (失败时),
            }
        """
        source_dir = Path(source_dir)
        # 防御性校验 paper_name，防路径穿越（调用方已校验，此处二次确认）
        from src.utils.naming import validate_paper_name, safe_child
        try:
            validate_paper_name(paper_name)
        except ValueError as e:
            return {"success": False, "paper_name": paper_name,
                    "error": f"Invalid paper_name: {e}"}

        md_path = self.locate_markdown(source_dir, method=method, stem=stem,
                                       backend=backend)
        if md_path is None:
            msg = f"未在 {source_dir} 找到正文 Markdown"
            logger.error(msg)
            return {"success": False, "paper_name": paper_name, "error": msg}

        dest_dir = safe_child(self.papers_dir, paper_name)
        # 覆盖保护：已存在则备份或报错，不无条件 rmtree
        if dest_dir.exists():
            if not overwrite:
                msg = (f"目标目录已存在，拒绝覆盖: {dest_dir} "
                       f"(传 overwrite=True 以备份后重建)")
                logger.error(msg)
                return {"success": False, "paper_name": paper_name, "error": msg}
            # 备份旧目录为 .bak_<timestamp>
            from datetime import datetime
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            bak = dest_dir.with_name(f"{dest_dir.name}__old_{ts}")
            dest_dir.rename(bak)
            logger.info(f"已备份旧目录: {dest_dir.name} -> {bak.name}")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_images = dest_dir / "images"
        dest_md = dest_dir / f"{paper_name}.md"

        # 1. 写入 <paper_name>.md，统一图片相对路径为 images/...
        md_content = md_path.read_text(encoding="utf-8")
        # MinerU 已用 ![](images/xxx) 形式，无需改写；若出现 ./images/ 也归一化
        md_content = md_content.replace("](./images/", "](images/")
        dest_md.write_text(md_content, encoding="utf-8")

        # 2. 复制 images/
        images_count = 0
        src_images = self.locate_images_dir(source_dir, md_path)
        if src_images and src_images.is_dir():
            dest_images.mkdir(parents=True, exist_ok=True)
            for img in src_images.iterdir():
                if img.is_file() and img.suffix.lower() in self.IMAGE_SUFFIXES:
                    shutil.copy2(img, dest_images / img.name)
                    images_count += 1

        logger.info(f"清理完成: {paper_name} -> {dest_md} ({len(md_content)} 字符, {images_count} 图)")
        return {
            "success": True,
            "paper_name": paper_name,
            "markdown_path": str(dest_md),
            "images_dir": str(dest_images) if images_count else "",
            "images_count": images_count,
            "char_count": len(md_content),
        }
