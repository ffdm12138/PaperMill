"""PDF fetch pipeline for v2 paper_raw attachment."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

import requests
from loguru import logger

from config.settings import MINERU_FETCH_MAX_BYTES
from src.discovery.models import normalize_doi
from src.fetch.access_policy import AccessMode, AccessPolicy
from src.fetch.models import FetchResult
from src.fetch.pdf_transport import TRANSPORT_POLICY, fetch_url_direct_then_proxy, sanitize_url_fields
from src.fetch.resolver_registry import build_resolvers
from src.fetch.resolvers.base import ResolveContext
from src.fetch.resolvers.url_safety import is_unsafe_url, validate_pdf_bytes


_build_resolvers = build_resolvers

def _make_attempt(resolver_name: str, status: str, result, *, reason: str = "") -> dict[str, Any]:
    """Build a rich per-attempt record with stable keys."""
    return sanitize_url_fields({
        "resolver": resolver_name,
        "status": status,
        "candidate_url": getattr(result, "candidate_url", "") or "",
        "final_url": getattr(result, "final_url", "") or result.pdf_url or "",
        "status_code": getattr(result, "status_code", None),
        "content_type": getattr(result, "content_type", "") or "",
        "reason": reason or result.error or "",
        "pdf_url": result.pdf_url or "",
        "landing_url": result.landing_url or "",
    })


def safe_doi_slug(doi: str) -> str:
    normalized = normalize_doi(doi)
    return re.sub(r"[^A-Za-z0-9._-]+", "_", normalized).strip("_") or "unknown_doi"


def _looks_like_pdf(response: requests.Response, url: str) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    return "pdf" in content_type or url.lower().split("?")[0].endswith(".pdf")


def _write_bytes_pdf(content: bytes, target: Path) -> tuple[Path, str]:
    # 任何写入的 PDF 都必须以 %PDF 魔数开头，拒绝伪 PDF（如 HTML 错误页）。
    error = validate_pdf_bytes(content)
    if error:
        raise ValueError(error)
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
    # 先检查源文件前几个字节是否为合法 PDF（非空 + %PDF 魔数）。
    # 空文件或 HTML 等伪 PDF 直接拒绝，不写入 target。
    try:
        with src.open("rb") as probe:
            head = probe.read(5)
    except OSError as exc:
        raise ValueError(f"cannot read source PDF: {exc}")
    error = validate_pdf_bytes(head)
    if error:
        raise ValueError(error)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    digest = hashlib.sha256()
    total = len(head)
    if total > MINERU_FETCH_MAX_BYTES:
        raise ValueError(f"PDF exceeds MINERU_FETCH_MAX_BYTES={MINERU_FETCH_MAX_BYTES}")
    try:
        with tmp.open("wb") as dest:
            digest.update(head)
            dest.write(head)
            with src.open("rb") as source:
                source.seek(len(head))
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


def _download_pdf(
    url: str,
    target: Path,
    *,
    timeout: int = 60,
    transport_attempts: list[dict[str, Any]] | None = None,
) -> tuple[Path, str]:
    # 下载前必须检查 unsafe host（sci-hub/libgen/...），直接拒绝。
    if is_unsafe_url(url):
        raise ValueError(f"unsafe source blocked: {url}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    digest = hashlib.sha256()
    total = 0
    try:
        with fetch_url_direct_then_proxy(
            url,
            expected_content="pdf",
            stream=True,
        ) as transport:
            if transport_attempts is not None:
                transport_attempts.extend(transport.safe_attempts)
            response = transport.response
            if response is None:
                raise ValueError(transport.error or "PDF transport failed")
            if response.status_code >= 400:
                raise ValueError(f"HTTP {response.status_code}")
            # redirect 后必须检查最终 URL：allow_redirects=True 可能跳到 unsafe host
            final_url = response.url or url
            if is_unsafe_url(final_url):
                raise ValueError(f"unsafe final URL blocked: {final_url}")
            if not _looks_like_pdf(response, final_url):
                raise ValueError(f"response is not a PDF: {response.headers.get('content-type', '')}")
            # 流式缓冲前 4 字节用于 %PDF 魔数检查（支持跨 chunk 分片）。
            # 若首个 chunk 只有 b"%P" 而第二个是 b"DF-1.4"，缓冲区会累积到
            # 至少 4 字节再判断，避免误拒合法 PDF。
            magic_buf = b""
            with tmp.open("wb") as fh:
                prefetched = bytes(getattr(response, "_mineru_prefetched_prefix", b"") or b"")
                if prefetched:
                    total += len(prefetched)
                    magic_buf = prefetched
                    digest.update(prefetched)
                    fh.write(prefetched)
                for chunk in response.iter_content(chunk_size=1024 * 64):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MINERU_FETCH_MAX_BYTES:
                        raise ValueError(f"PDF exceeds MINERU_FETCH_MAX_BYTES={MINERU_FETCH_MAX_BYTES}")
                    if len(magic_buf) < 4:
                        magic_buf += chunk
                        if len(magic_buf) >= 4:
                            if not magic_buf[:4].startswith(b"%PDF"):
                                raise ValueError("response content is not a valid PDF (missing %PDF magic)")
                            # 魔数通过后，把缓冲区的所有数据刷入
                            digest.update(magic_buf)
                            fh.write(magic_buf)
                        continue
                    digest.update(chunk)
                    fh.write(chunk)
                # 循环结束：检查是否为空响应或流结束仍不足 4 字节
                if total == 0:
                    raise ValueError("empty PDF response")
                if len(magic_buf) < 4:
                    raise ValueError("response content is not a valid PDF (incomplete body)")
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
    source_record: dict[str, Any] | None = None,
) -> FetchResult:
    """Resolve and download a PDF into a caller-owned temporary folder.

    *source_record* is an optional runtime-only raw record from the
    metadata source (CrossRef/OpenAlex/Unpaywall).  It is passed directly
    into ``ResolveContext.source_record`` and never persisted to metadata.
    The caller is responsible for loading it from the appropriate
    ``source.raw_record_path`` file before calling this function.
    """
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
        source_record=source_record or {},
        access_policy=policy,
    )
    output_root = Path(output_root or ".")
    target = output_root / f"{safe_doi_slug(normalized)}.pdf"
    chain: list[str] = []
    last_error = ""
    attempts: list[dict[str, Any]] = []
    transport_attempts: list[dict[str, Any]] = []
    # Legacy not_configured_resolvers from policy (generic mechanism).  The
    # current --resolver auto does NOT use this for header_based — it is
    # always in the active chain, defaulting to doi.org.
    not_configured = list((policy.extra or {}).get("not_configured_resolvers") or [])

    for resolver in resolvers:
        chain.append(resolver.name)
        result = resolver.resolve(ctx)
        result.resolver_chain = list(chain)
        result.resolver = resolver.name
        result.access_mode = policy.mode.value
        if result.transport_attempts:
            transport_attempts.extend(sanitize_url_fields(result.transport_attempts))
        if not result.success:
            last_error = result.error or last_error
            attempts.append(_make_attempt(resolver.name, "failed", result, reason=result.error or ""))
            continue
        if result.requires_user_action:
            result.output_path = ""
            result.attempts = attempts + [_make_attempt(resolver.name, "success", result, reason="requires_user_action")]
            result.transport_attempts = sanitize_url_fields(list(transport_attempts))
            result.not_configured_resolvers = list(not_configured)
            return result
        if dry_run:
            result.output_path = ""
            result.attempts = attempts + [_make_attempt(resolver.name, "success", result, reason="dry_run")]
            result.transport_attempts = sanitize_url_fields(list(transport_attempts))
            result.not_configured_resolvers = list(not_configured)
            return result
        try:
            # prefer bytes already fetched by the resolver (e.g. header_based,
            # which uses authorized headers) over re-downloading via pdf_url
            # with a headerless request -- avoids the second no-auth download.
            if result.raw and result.raw.get("content"):
                pdf_path, sha = _write_bytes_pdf(result.raw["content"], target)
                result.raw.pop("content", None)
            elif result.pdf_url:
                # resolver 返回的 pdf_url（含 OA API 来源）下载前二次检查 unsafe host
                if is_unsafe_url(result.pdf_url):
                    last_error = f"unsafe source blocked: {result.pdf_url}"
                    attempts.append(_make_attempt(resolver.name, "failed", result, reason=last_error))
                    continue
                try:
                    pdf_path, sha = _download_pdf(
                        result.pdf_url,
                        target,
                        timeout=policy.timeout_seconds,
                        transport_attempts=transport_attempts,
                    )
                except TypeError as exc:
                    if "transport_attempts" not in str(exc):
                        raise
                    pdf_path, sha = _download_pdf(
                        result.pdf_url,
                        target,
                        timeout=policy.timeout_seconds,
                    )
            elif result.output_path and Path(result.output_path).exists():
                pdf_path, sha = _copy_pdf(Path(result.output_path), target)
            else:
                last_error = "resolver returned no downloadable PDF"
                attempts.append(_make_attempt(resolver.name, "failed", result, reason=last_error))
                continue
            result.output_path = pdf_path.as_posix()
            result.sha256 = sha
            result.attempts = attempts + [_make_attempt(resolver.name, "success", result)]
            result.transport_attempts = sanitize_url_fields(list(transport_attempts))
            result.not_configured_resolvers = list(not_configured)
            return result
        except Exception as exc:
            logger.warning("download failed from {} for {!r}: {}", resolver.name, doi, exc)
            # If the OA resolver returned a landing page (not a direct PDF),
            # try to resolve it through multi-level landing page parsing.
            maybe_landing = (result.metadata or {}).get("maybe_landing_page")
            if maybe_landing and result.pdf_url and "not a PDF" in str(exc):
                from src.fetch.resolvers.landing_page import try_resolve_landing_to_pdf
                content, landing_error = try_resolve_landing_to_pdf(
                    result.pdf_url,
                    timeout_seconds=policy.timeout_seconds,
                    transport_attempts=transport_attempts,
                )
                if content is not None:
                    try:
                        pdf_path, sha = _write_bytes_pdf(content, target)
                        result.output_path = pdf_path.as_posix()
                        result.sha256 = sha
                        result.is_direct_pdf = False
                        result.attempts = attempts + [_make_attempt(resolver.name, "success", result, reason="resolved from landing page")]
                        result.transport_attempts = sanitize_url_fields(list(transport_attempts))
                        result.not_configured_resolvers = list(not_configured)
                        return result
                    except Exception as landing_exc:
                        logger.warning(
                            "landing page resolution failed for {}: {}", result.pdf_url, landing_exc
                        )
            last_error = str(exc)
            attempts.append(_make_attempt(resolver.name, "failed", result, reason=str(exc)))
            continue

    # not_configured_resolvers is a legacy/generic policy marker.
    # Current fetch_pdf_for_paper_raw.py --resolver auto does not use it for
    # header_based because header_based defaults to https://doi.org/{doi}.
    return FetchResult(
        doi=normalized,
        error=last_error or "no PDF found",
        resolver_chain=chain,
        access_mode=policy.mode.value,
        attempts=attempts,
        transport_attempts=sanitize_url_fields(list(transport_attempts)),
        not_configured_resolvers=not_configured,
    )
