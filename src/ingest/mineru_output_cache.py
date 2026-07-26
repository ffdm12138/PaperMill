"""Verified local cache for reusable MinerU raw output."""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from config.settings import MINERU_OUTPUT_CACHE_DIR
from src.cleaner import MinerUOutputCleaner
from src.file_fingerprint import compute_file_hashes, compute_sha256
from src.path_utils import normalize_repo_path
from src.ingest.asset_manifest import write_asset_manifest
from src.ingest.import_status import CONVERTED, write_import_status
from src.utils.fs import replace_images_dir
from src.utils.atomic_io import atomic_write_json, atomic_write_text


CACHE_MANIFEST = "mineru_output_cache.json"


@dataclass(frozen=True)
class MinerUOutputCacheKey:
    pdf_md5: str
    pdf_sha256: str
    pdf_file_size: int
    backend: str
    method: str
    lang: str
    effort: str


@dataclass
class MinerUOutputCacheHit:
    ok: bool
    reason: str
    cache_dir: Path | None
    output_dir: Path | None
    manifest_path: Path | None
    markdown_path: Path | None
    images_dir: Path | None
    pdf_md5: str
    pdf_sha256: str
    pdf_file_size: int
    state: str = "miss"


class MinerUOutputCache:
    def __init__(
        self,
        cache_dir: str | Path = MINERU_OUTPUT_CACHE_DIR,
        cleaner: MinerUOutputCleaner | None = None,
    ):
        self.cache_dir = Path(cache_dir)
        self.cleaner = cleaner or MinerUOutputCleaner()

    def key_for_pdf(
        self,
        pdf_path: Path,
        *,
        backend: str,
        method: str,
        lang: str,
        effort: str,
    ) -> MinerUOutputCacheKey:
        hashes = compute_file_hashes(Path(pdf_path))
        return MinerUOutputCacheKey(
            pdf_md5=str(hashes["md5"]).lower(),
            pdf_sha256=str(hashes["sha256"]).lower(),
            pdf_file_size=int(hashes["file_size"]),
            backend=str(backend),
            method=str(method),
            lang=str(lang),
            effort=str(effort),
        )

    def cache_dir_for_key(self, key: MinerUOutputCacheKey) -> Path:
        return self.cache_dir / key.pdf_md5 / self._params_dir(key.backend, key.method, key.lang, key.effort)

    def find(
        self,
        pdf_path: Path,
        *,
        backend: str,
        method: str,
        lang: str,
        effort: str,
        stem: str,
    ) -> MinerUOutputCacheHit:
        key = self.key_for_pdf(Path(pdf_path), backend=backend, method=method, lang=lang, effort=effort)
        cache_dir = self.cache_dir_for_key(key)
        manifest_path = cache_dir / CACHE_MANIFEST
        if manifest_path.exists():
            return self._find_from_manifest(
                key,
                cache_dir,
                manifest_path,
                backend=backend,
                method=method,
                lang=lang,
                effort=effort,
                stem=stem,
            )
        return self._miss(
            key,
            cache_dir,
            None,
            "cache manifest missing",
            state="unverifiable",
        )

    def register(
        self,
        *,
        source_output_dir: Path,
        pdf_path: Path,
        source_paper_raw_id: str,
        backend: str,
        method: str,
        lang: str,
        effort: str,
        runner: str = "",
        api_url: str = "",
    ) -> dict[str, Any]:
        key = self.key_for_pdf(Path(pdf_path), backend=backend, method=method, lang=lang, effort=effort)
        target_dir = self.cache_dir_for_key(key)
        existing = self.find(
            Path(pdf_path),
            backend=backend,
            method=method,
            lang=lang,
            effort=effort,
            stem=Path(pdf_path).stem,
        )
        if existing.ok and existing.manifest_path:
            return {
                "registered": False,
                "reason": "existing verified cache reused",
                "cache_dir": normalize_repo_path(target_dir),
                "manifest_path": normalize_repo_path(existing.manifest_path),
            }

        source_output_dir = Path(source_output_dir)
        tmp_dir = target_dir.parent / f".cache_tmp_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        shutil.rmtree(tmp_dir, ignore_errors=True)
        try:
            tmp_dir.mkdir(parents=True, exist_ok=True)
            if source_output_dir.exists():
                shutil.copytree(source_output_dir, tmp_dir / source_output_dir.name)
            md_path = self.cleaner.locate_markdown(
                tmp_dir,
                method=method,
                stem=Path(pdf_path).stem,
                backend=backend,
            )
            if md_path is None:
                md_path = self.cleaner.locate_markdown(tmp_dir, method=method, backend=backend)
            if md_path is None:
                raise FileNotFoundError("MinerU output cache registration could not locate Markdown")
            images_dir = self.cleaner.locate_images_dir(tmp_dir, md_path)
            if images_dir is None or not images_dir.is_dir():
                raise FileNotFoundError("MinerU output cache registration could not locate images directory")
            manifest = {
                "schema_version": "1.0",
                "kind": "mineru_output_cache",
                "status": "converted",
                "pdf_md5": key.pdf_md5,
                "pdf_sha256": key.pdf_sha256,
                "pdf_file_size": key.pdf_file_size,
                "backend": backend,
                "method": method,
                "lang": lang,
                "effort": effort,
                "source_pdf_name": Path(pdf_path).name,
                "source_paper_raw_id": source_paper_raw_id,
                "output_dir": ".",
                "markdown_relpath": md_path.relative_to(tmp_dir).as_posix(),
                "images_dir_relpath": images_dir.relative_to(tmp_dir).as_posix(),
                "mineru_runner": runner,
                "api_url": api_url,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            atomic_write_json(tmp_dir / CACHE_MANIFEST, manifest, indent=2)
            shutil.rmtree(target_dir, ignore_errors=True)
            target_dir.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_dir, target_dir)
            return {
                "registered": True,
                "reason": "cache registered",
                "cache_dir": normalize_repo_path(target_dir),
                "manifest_path": normalize_repo_path(target_dir / CACHE_MANIFEST),
            }
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def restore_to_paper_raw(
        self,
        *,
        hit: MinerUOutputCacheHit,
        folder: Path,
        paper_raw_id: str,
        backend: str,
        method: str,
        lang: str,
        effort: str,
        output_cache_enabled: bool = True,
    ) -> dict[str, Any]:
        if not hit.ok or hit.markdown_path is None or hit.images_dir is None:
            raise ValueError(f"cannot restore from cache miss: {hit.reason}")
        folder = Path(folder)
        pdf = folder / f"{paper_raw_id}.pdf"
        target_md = folder / f"{paper_raw_id}.md"
        images_target = folder / "images"

        text = hit.markdown_path.read_text(encoding="utf-8").replace("](./images/", "](images/")
        self._write_text_atomic(target_md, text)
        images_count = self._replace_images_dir(hit.images_dir, images_target)
        markdown_sha = compute_sha256(target_md)
        manifest_path = folder / f"{paper_raw_id}.conversion.json"
        manifest = {
            "schema_version": "1.0",
            "status": "converted",
            "paper_number": paper_raw_id if paper_raw_id.isdigit() and len(paper_raw_id) == 16 else "",
            "paper_raw_id": paper_raw_id if paper_raw_id.isdigit() and len(paper_raw_id) == 16 else "",
            "pdf_md5": hit.pdf_md5,
            "pdf_sha256": hit.pdf_sha256,
            "pdf_file_size": hit.pdf_file_size,
            "markdown_path": f"{paper_raw_id}.md",
            "markdown_sha256": markdown_sha,
            "images_dir": "images",
            "images_count": images_count,
            "backend": backend,
            "method": method,
            "lang": lang,
            "effort": effort,
            "runner": "",
            "api_url": "",
            "output_dir": normalize_repo_path(hit.output_dir or hit.cache_dir or ""),
            "conversion_source": "mineru_output_cache",
            "restored_from_output_cache": True,
            "output_cache_enabled": output_cache_enabled,
            "output_cache_hit": True,
            "output_cache_dir": normalize_repo_path(hit.cache_dir or ""),
            "output_cache_manifest": normalize_repo_path(hit.manifest_path or ""),
            "converted_at": datetime.now().isoformat(timespec="seconds"),
        }
        atomic_write_json(manifest_path, manifest, indent=2)
        write_import_status(
            folder,
            CONVERTED,
            reason="MinerU conversion restored from output cache",
            extra={
                "paper_number": paper_raw_id,
                "paper_raw_id": paper_raw_id,
                "pdf_md5": hit.pdf_md5,
                "pdf_sha256": hit.pdf_sha256,
                "markdown_sha256": markdown_sha,
                "restored_from_output_cache": True,
                "output_cache_dir": normalize_repo_path(hit.cache_dir or ""),
            },
        )
        write_asset_manifest(folder, prefix=paper_raw_id, paper_number=paper_raw_id, stage="paper_raw")
        return {
            "markdown": str(target_md),
            "images_dir": str(images_target),
            "images_count": images_count,
            "conversion_manifest": str(manifest_path),
            "manifest": manifest,
            "pdf_path": str(pdf),
        }

    def _find_from_manifest(
        self,
        key: MinerUOutputCacheKey,
        cache_dir: Path,
        manifest_path: Path,
        *,
        backend: str,
        method: str,
        lang: str,
        effort: str,
        stem: str,
    ) -> MinerUOutputCacheHit:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return self._miss(key, cache_dir, manifest_path, f"cache manifest unreadable: {exc}", state="unverifiable")
        mismatches = self._manifest_mismatches(manifest, key, backend=backend, method=method, lang=lang, effort=effort)
        if mismatches:
            return self._miss(key, cache_dir, manifest_path, "; ".join(mismatches), state="stale_params")
        md_path = self._manifest_child(cache_dir, manifest.get("markdown_relpath"))
        images_dir = self._manifest_child(cache_dir, manifest.get("images_dir_relpath"))
        if md_path is None or not md_path.is_file():
            md_path = self.cleaner.locate_markdown(cache_dir, method=method, stem=stem, backend=backend)
        if md_path is None or not md_path.is_file():
            return self._miss(key, cache_dir, manifest_path, "cache markdown missing", state="unverifiable")
        if images_dir is None or not images_dir.is_dir():
            images_dir = self.cleaner.locate_images_dir(cache_dir, md_path)
        if images_dir is None or not images_dir.is_dir():
            return self._miss(key, cache_dir, manifest_path, "cache images directory missing", state="unverifiable")
        return MinerUOutputCacheHit(
            ok=True,
            reason="pdf md5/sha256/file size and conversion parameters matched",
            cache_dir=cache_dir,
            output_dir=cache_dir,
            manifest_path=manifest_path,
            markdown_path=md_path,
            images_dir=images_dir,
            pdf_md5=key.pdf_md5,
            pdf_sha256=key.pdf_sha256,
            pdf_file_size=key.pdf_file_size,
            state="hit",
        )

    def _manifest_mismatches(
        self,
        manifest: dict[str, Any],
        key: MinerUOutputCacheKey,
        *,
        backend: str,
        method: str,
        lang: str,
        effort: str,
    ) -> list[str]:
        checks = {
            "pdf_md5": key.pdf_md5,
            "pdf_sha256": key.pdf_sha256,
            "pdf_file_size": key.pdf_file_size,
            "backend": backend,
            "method": method,
            "lang": lang,
            "effort": effort,
        }
        out = []
        if manifest.get("status") != "converted":
            out.append("cache status is not converted")
        for name, expected in checks.items():
            actual = manifest.get(name)
            if str(actual) != str(expected):
                out.append(f"{name} mismatch")
        return out

    def _manifest_child(self, base: Path, rel: Any) -> Path | None:
        if not rel:
            return None
        candidate = base / str(rel)
        try:
            candidate.resolve().relative_to(base.resolve())
        except ValueError:
            return None
        return candidate

    def _miss(
        self,
        key: MinerUOutputCacheKey,
        cache_dir: Path | None,
        manifest_path: Path | None,
        reason: str,
        *,
        state: str = "miss",
    ) -> MinerUOutputCacheHit:
        return MinerUOutputCacheHit(
            ok=False,
            reason=reason,
            cache_dir=cache_dir,
            output_dir=None,
            manifest_path=manifest_path,
            markdown_path=None,
            images_dir=None,
            pdf_md5=key.pdf_md5,
            pdf_sha256=key.pdf_sha256,
            pdf_file_size=key.pdf_file_size,
            state=state,
        )

    def _replace_images_dir(self, images_source: Path, images_target: Path) -> int:
        return replace_images_dir(images_source, images_target)

    def _write_text_atomic(self, path: Path, text: str) -> None:
        atomic_write_text(path, text, fsync=False)

    def _params_dir(self, backend: str, method: str, lang: str, effort: str) -> str:
        parts = [backend, method, lang, effort]
        return "__".join(str(p).replace("/", "_").replace("\\", "_") for p in parts)
