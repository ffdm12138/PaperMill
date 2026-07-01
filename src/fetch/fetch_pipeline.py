"""PDF fetch pipeline for v2 paper_raw attachment."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import requests
from loguru import logger

from config.settings import FETCH_PROXY, MINERU_FETCH_MAX_BYTES
from src.discovery.models import normalize_doi
from src.fetch.access_policy import AccessMode, AccessPolicy
from src.fetch.models import FetchResult
from src.fetch.resolver_registry import build_resolvers
from src.fetch.resolvers.base import ResolveContext


_build_resolvers = build_resolvers

if FETCH_PROXY:
    os.environ.setdefault("HTTP_PROXY", FETCH_PROXY)
    os.environ.setdefault("HTTPS_PROXY", FETCH_PROXY)
    logger.info(f"fetch proxy enabled: {FETCH_PROXY}")


def safe_doi_slug(doi: str) -> str:
    normalized = normalize_doi(doi)
    return re.sub(r"[^A-Za-z0-9._-]+", "_", normalized).strip("_") or "unknown_doi"


def _looks_like_pdf(response: requests.Response, url: str) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    return "pdf" in content_type or url.lower().split("?")[0].endswith(".pdf")


def _write_bytes_pdf(content: bytes, target: Path) -> tuple[Path, str]:
    if len(content) > MINERU_FETCH_MAX_BYTES:
        raise ValueError(f"PDF exceeds MINERU_FETCH_MAX_BYTES={MINERU_FETCH_MAX_BYTES}")
    target.parent.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(content).hexdigest()
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_bytes(content)
        os.replace(tmp, target)
        return target, sha
    finally:
        tmp.unlink(missing_ok=True)


def _copy_pdf(src: Path, target: Path) -> tuple[Path, str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    digest = hashlib.sha256()
    total = 0
    try:
        with src.open("rb") as source, tmp.open("wb") as dest:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                total += len(chunk)
                if total > MINERU_FETCH_MAX_BYTES:
                    raise ValueError(f"PDF exceeds MINERU_FETCH_MAX_BYTES={MINERU_FETCH_MAX_BYTES}")
                digest.update(chunk)
                dest.write(chunk)
        os.replace(tmp, target)
        return target, digest.hexdigest()
    finally:
        tmp.unlink(missing_ok=True)


def _download_pdf(url: str, target: Path) -> tuple[Path, str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    digest = hashlib.sha256()
    total = 0
    try:
        with requests.get(url, stream=True, timeout=60) as response:
            response.raise_for_status()
            if not _looks_like_pdf(response, response.url or url):
                raise ValueError(f"response is not a PDF: {response.headers.get('content-type', '')}")
            with tmp.open("wb") as fh:
                for chunk in response.iter_content(chunk_size=1024 * 64):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MINERU_FETCH_MAX_BYTES:
                        raise ValueError(f"PDF exceeds MINERU_FETCH_MAX_BYTES={MINERU_FETCH_MAX_BYTES}")
                    digest.update(chunk)
                    fh.write(chunk)
        os.replace(tmp, target)
        return target, digest.hexdigest()
    finally:
        tmp.unlink(missing_ok=True)


def fetch_pdf(
    doi: str,
    domain_id: str | None = None,
    output_root: Path | str | None = None,
    dry_run: bool = False,
    access_policy: AccessPolicy | None = None,
    title: str = "",
    year: int | None = None,
    metadata: dict | None = None,
) -> FetchResult:
    """Resolve and download a PDF into a caller-owned temporary folder."""
    normalized = normalize_doi(doi)
    if not normalized:
        return FetchResult(doi=doi, error="doi is required")

    policy = access_policy or AccessPolicy(mode=AccessMode.OA_ONLY)
    resolvers = _build_resolvers(policy)
    ctx = ResolveContext(
        doi=normalized,
        title=title,
        year=year,
        domain_id=domain_id,
        metadata=metadata or {},
        access_policy=policy,
    )
    output_root = Path(output_root or ".")
    target = output_root / f"{safe_doi_slug(normalized)}.pdf"
    chain: list[str] = []
    last_error = ""

    for resolver in resolvers:
        chain.append(resolver.name)
        result = resolver.resolve(ctx)
        result.resolver_chain = list(chain)
        result.resolver = resolver.name
        result.access_mode = policy.mode.value
        if not result.success:
            last_error = result.error or last_error
            continue
        if result.requires_user_action:
            result.output_path = ""
            return result
        if dry_run:
            result.output_path = ""
            return result
        try:
            if result.pdf_url:
                pdf_path, sha = _download_pdf(result.pdf_url, target)
            elif result.output_path and Path(result.output_path).exists():
                pdf_path, sha = _copy_pdf(Path(result.output_path), target)
            elif result.raw and result.raw.get("content"):
                pdf_path, sha = _write_bytes_pdf(result.raw["content"], target)
                result.raw.pop("content", None)
            else:
                last_error = "resolver returned no downloadable PDF"
                continue
            result.output_path = pdf_path.as_posix()
            result.sha256 = sha
            return result
        except Exception as exc:
            logger.warning("download failed from {} for {!r}: {}", resolver.name, doi, exc)
            last_error = str(exc)
            continue

    return FetchResult(
        doi=normalized,
        error=last_error or "no PDF found",
        resolver_chain=chain,
        access_mode=policy.mode.value,
    )
