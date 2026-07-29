"""PDF fetch pipeline for v2 paper_raw attachment."""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from config.settings import MINERU_FETCH_MAX_BYTES
from src.utils.identifiers import normalize_doi
from src.fetch.access_policy import AccessMode, AccessPolicy
from src.fetch.models import FetchResult
from src.fetch.pdf_transport import TRANSPORT_POLICY, fetch_url_direct_then_proxy, sanitize_for_persistence
from src.fetch.resolver_registry import build_resolvers
from src.fetch.resolvers.base import ResolveContext
from src.fetch.resolvers.url_safety import (
    is_pdf_response,
    is_unsafe_url,
    looks_like_pdf_url,
    validate_pdf_bytes,
)


_build_resolvers = build_resolvers

def _make_attempt(resolver_name: str, status: str, result, *, reason: str = "") -> dict[str, Any]:
    """Build a rich per-attempt record with stable keys."""
    return sanitize_for_persistence({
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
            direct_timeout=timeout,
            proxy_timeout=timeout,
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
            if not (is_pdf_response(response) or looks_like_pdf_url(final_url)):
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


@dataclass
class _DownloadCandidate:
    """One URL the pipeline may try, plus why it failed if it did."""

    url: str
    is_direct_pdf: bool = True
    error: str = ""


def _download_candidates(result: FetchResult) -> list[_DownloadCandidate]:
    """Return the ranked URLs to try for *result*.

    Resolvers that know about several OA locations publish them in
    ``pdf_candidates``; the rest publish a single ``pdf_url``.  Both are
    normalized here so the download loop has one shape.
    """
    ranked = [
        _DownloadCandidate(
            url=str(item.get("url") or "").strip(),
            is_direct_pdf=bool(item.get("is_direct_pdf", True)),
        )
        for item in (result.pdf_candidates or [])
        if isinstance(item, dict) and str(item.get("url") or "").strip()
    ]
    if ranked:
        return ranked
    if result.pdf_url:
        return [_DownloadCandidate(url=result.pdf_url, is_direct_pdf=bool(result.is_direct_pdf))]
    return []


def _candidate_error(candidates: list[_DownloadCandidate]) -> str:
    """Return the first real failure reason across *candidates*."""
    for candidate in candidates:
        if candidate.error:
            return candidate.error
    return ""


def _try_download_candidates(
    candidates: list[_DownloadCandidate],
    target: Path,
    *,
    policy: AccessPolicy,
    transport_attempts: list[dict[str, Any]],
    doi: str,
    resolver_name: str,
    maybe_landing: bool,
) -> tuple[Path, str, str, bool] | None:
    """Try each candidate in order; return the first that yields PDF bytes.

    Returns ``(path, sha256, url_used, resolved_from_landing_page)`` on
    success, or ``None`` when every candidate failed.  Each candidate records
    its own failure reason so the caller can report the real cause rather than
    whichever error happened to come last.
    """
    for candidate in candidates:
        if is_unsafe_url(candidate.url):
            candidate.error = f"unsafe source blocked: {candidate.url}"
            continue
        try:
            pdf_path, sha = _download_pdf(
                candidate.url,
                target,
                timeout=policy.timeout_seconds,
                transport_attempts=transport_attempts,
            )
            return pdf_path, sha, candidate.url, False
        except Exception as exc:
            logger.warning("download failed from {} for {!r}: {}", resolver_name, doi, exc)
            candidate.error = str(exc)

        # A location that is not a direct PDF is a landing page; parse it for
        # the real PDF link rather than discarding the candidate.
        if not (maybe_landing or not candidate.is_direct_pdf):
            continue
        if "not a PDF" not in candidate.error and "%PDF" not in candidate.error:
            continue
        from src.fetch.resolvers.landing_page import try_resolve_landing_to_pdf
        content, landing_error = try_resolve_landing_to_pdf(
            candidate.url,
            timeout_seconds=policy.timeout_seconds,
            transport_attempts=transport_attempts,
        )
        if content is None:
            continue
        try:
            pdf_path, sha = _write_bytes_pdf(content, target)
            return pdf_path, sha, candidate.url, True
        except Exception as landing_exc:
            logger.warning("landing page resolution failed for {}: {}", candidate.url, landing_exc)
            candidate.error = str(landing_exc)
    return None


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
    skipped: list[str] = []
    last_error = ""
    attempts: list[dict[str, Any]] = []
    transport_attempts: list[dict[str, Any]] = []

    for resolver in resolvers:
        # A resolver that cannot serve this DOI is skipped before any I/O and
        # stays out of both the chain and the attempt log, so the record shows
        # real failures rather than constant no-ops.
        if not resolver.applies_to(ctx):
            skipped.append(resolver.name)
            continue
        chain.append(resolver.name)
        result = resolver.resolve(ctx)
        result.resolver_chain = list(chain)
        result.resolver = resolver.name
        result.access_mode = policy.mode.value
        if result.transport_attempts:
            transport_attempts.extend(sanitize_for_persistence(result.transport_attempts))
        if not result.success:
            last_error = result.error or last_error
            attempts.append(_make_attempt(resolver.name, "failed", result, reason=result.error or ""))
            continue
        if result.requires_user_action:
            result.output_path = ""
            result.attempts = attempts + [_make_attempt(resolver.name, "success", result, reason="requires_user_action")]
            result.transport_attempts = sanitize_for_persistence(list(transport_attempts))
            result.resolvers_skipped = list(skipped)
            return result
        if dry_run:
            result.output_path = ""
            result.attempts = attempts + [_make_attempt(resolver.name, "success", result, reason="dry_run")]
            result.transport_attempts = sanitize_for_persistence(list(transport_attempts))
            result.resolvers_skipped = list(skipped)
            return result
        # prefer bytes already fetched by the resolver (e.g. header_based,
        # which uses authorized headers) over re-downloading via pdf_url
        # with a headerless request -- avoids the second no-auth download.
        if result.raw and result.raw.get("content"):
            try:
                pdf_path, sha = _write_bytes_pdf(result.raw["content"], target)
                result.raw.pop("content", None)
            except Exception as exc:
                logger.warning("download failed from {} for {!r}: {}", resolver.name, doi, exc)
                last_error = str(exc)
                attempts.append(_make_attempt(resolver.name, "failed", result, reason=str(exc)))
                continue
            result.output_path = pdf_path.as_posix()
            result.sha256 = sha
            result.attempts = attempts + [_make_attempt(resolver.name, "success", result)]
            result.transport_attempts = sanitize_for_persistence(list(transport_attempts))
            result.resolvers_skipped = list(skipped)
            return result

        # A resolver may report several places the same paper can be
        # downloaded from, already ranked so that reachable repository copies
        # come before publisher copies that refuse this egress. Walk the whole
        # list: a 403 on the publisher must not end the resolver's turn.
        candidates = _download_candidates(result)
        if candidates:
            downloaded = _try_download_candidates(
                candidates,
                target,
                policy=policy,
                transport_attempts=transport_attempts,
                doi=doi,
                resolver_name=resolver.name,
                maybe_landing=bool((result.metadata or {}).get("maybe_landing_page")),
            )
            if downloaded is None:
                last_error = _candidate_error(candidates) or "no candidate yielded a PDF"
                attempts.append(_make_attempt(resolver.name, "failed", result, reason=last_error))
                continue
            pdf_path, sha, used_url, from_landing = downloaded
            result.pdf_url = used_url
            if from_landing:
                result.is_direct_pdf = False
            result.output_path = pdf_path.as_posix()
            result.sha256 = sha
            reason = "resolved from landing page" if from_landing else ""
            result.attempts = attempts + [_make_attempt(resolver.name, "success", result, reason=reason)]
            result.transport_attempts = sanitize_for_persistence(list(transport_attempts))
            result.resolvers_skipped = list(skipped)
            return result

        if result.output_path and Path(result.output_path).exists():
            try:
                pdf_path, sha = _copy_pdf(Path(result.output_path), target)
            except Exception as exc:
                logger.warning("download failed from {} for {!r}: {}", resolver.name, doi, exc)
                last_error = str(exc)
                attempts.append(_make_attempt(resolver.name, "failed", result, reason=str(exc)))
                continue
            result.output_path = pdf_path.as_posix()
            result.sha256 = sha
            result.attempts = attempts + [_make_attempt(resolver.name, "success", result)]
            result.transport_attempts = sanitize_for_persistence(list(transport_attempts))
            result.resolvers_skipped = list(skipped)
            return result

        last_error = "resolver returned no downloadable PDF"
        attempts.append(_make_attempt(resolver.name, "failed", result, reason=last_error))
        continue

    return FetchResult(
        doi=normalized,
        error=last_error or "no PDF found",
        resolver_chain=chain,
        resolvers_skipped=list(skipped),
        access_mode=policy.mode.value,
        attempts=attempts,
        transport_attempts=sanitize_for_persistence(list(transport_attempts)),
    )
