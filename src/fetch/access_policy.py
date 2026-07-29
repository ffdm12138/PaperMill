"""PDF 获取策略与候选分类。

两部分：``AccessPolicy`` 控制启用哪些 resolver 后端；
``classify_pdf_fetch_candidate`` 判定一个 paper_raw 工作区是否该进入抓取。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from src.metadata.quality import is_valid_normalized_doi
from src.metadata.source_records import FETCH_RESULT_FILENAME
from src.utils.identifiers import normalize_doi, validate_paper_raw_id
from src.utils.jsonio import read_json


class AccessMode(str, Enum):
    OA_ONLY = "oa_only"
    INSTITUTIONAL = "institutional"
    BROWSER_ASSISTED = "browser_assisted"
    LOCAL_MANUAL = "local_manual"
    CUSTOM = "custom"


@dataclass
class AccessPolicy:
    """PDF 获取策略：控制启用哪些 resolver、超时、行为。

    规则：
    - OA_ONLY：仅真正开放获取 / 合法公开来源。
    - INSTITUTIONAL：OA_ONLY + 机构/TDM 通道（需 token 或机构订阅）。
    - BROWSER_ASSISTED：OA_ONLY + 浏览器辅助（需用户操作）。
    - LOCAL_MANUAL：仅本地文件。
    - CUSTOM：OA_ONLY + 明确配置的 custom/header-based/local/TDM resolver；不得包含 Sci-Hub。
    """

    mode: AccessMode = AccessMode.OA_ONLY
    allow_browser: bool = False
    allow_institutional: bool = False
    allow_publisher_tdm: bool = True
    allow_preprints: bool = True
    allow_manual_import: bool = True
    allow_custom_resolvers: bool = False
    custom_resolvers: list[str] = field(default_factory=list)
    max_attempts_per_resolver: int = 1
    timeout_seconds: int = 60
    user_agent: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def enabled_resolver_names(self) -> list[str]:
        """根据 mode 返回该策略下启用的 resolver 名称列表。"""
        explicit = (self.extra or {}).get("resolver_names")
        if explicit:
            return list(explicit)
        if self.mode == AccessMode.OA_ONLY:
            return self._oa_resolvers()
        if self.mode == AccessMode.INSTITUTIONAL:
            return self._oa_resolvers() + self._institutional_resolvers()
        if self.mode == AccessMode.BROWSER_ASSISTED:
            return self._oa_resolvers() + ["browser_assisted"]
        if self.mode == AccessMode.LOCAL_MANUAL:
            return ["local_manual"]
        if self.mode == AccessMode.CUSTOM:
            base = list(self._oa_resolvers())
            if self.allow_publisher_tdm:
                base += self._tdm_resolvers()
            if self.allow_custom_resolvers:
                base += list(self.custom_resolvers)
            return base
        return []

    @staticmethod
    def _oa_resolvers() -> list[str]:
        """真正开放获取 / 合法公开来源（无需 token、无需付费墙绕过）。

        ``original_link`` 居首：优先尝试 metadata 中已有的原始链接。
        """
        return ["original_link", "unpaywall", "openalex", "semantic_scholar", "arxiv",
                "publisher_oa", "springer_direct",
                "sciengine_direct",
                "biorxiv", "pmc_oa"]

    @staticmethod
    def _tdm_resolvers() -> list[str]:
        """Publisher TDM 通道（可能需要免费 token，属于机构/授权语义）。"""
        return ["wiley_tdm", "elsevier_tdm"]

    @staticmethod
    def _institutional_resolvers() -> list[str]:
        return ["publisher_tdm", "institutional_browser"] + AccessPolicy._tdm_resolvers()

    def clone_with(self, **overrides) -> AccessPolicy:
        kwargs = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        kwargs.update(overrides)
        return AccessPolicy(**kwargs)


BLOCKED_FETCH_STATUSES = {
    "ready_for_commit",
    "catalog_ready",
    "committed",
    "imported",
    "quarantined_duplicate",
    "possible_duplicate",
}


@dataclass
class FetchCandidateStatus:
    paper_number: str
    folder: Path
    status: str
    reason: str = ""
    has_metadata: bool = False
    has_pdf: bool = False
    doi: str = ""
    import_status: str = ""
    metadata: dict[str, Any] | None = None

    @property
    def eligible(self) -> bool:
        return self.status == "planned"

    def to_item(self) -> dict[str, Any]:
        return {
            "paper_number": self.paper_number,
            "paper_raw_id": self.paper_number,
            "status": self.status,
            "reason": self.reason,
            "has_metadata": self.has_metadata,
            "has_pdf": self.has_pdf,
            "doi": self.doi,
            "import_status": self.import_status,
        }


@dataclass(frozen=True)
class FetchSelection:
    """Which eligible workspaces a batch run should actually attempt.

    Eligibility (``classify_pdf_fetch_candidate``) answers "can this
    workspace be fetched at all"; selection answers "should this run spend
    time on it now".  They are separate because the backlog is far larger
    than one run: 2182 of 2332 workspaces were eligible while only 514 had
    ever been attempted, so a run needs to be able to reach fresh work
    without first replaying every known-hard failure.

    ``doi_prefixes`` is matched case-insensitively against the registrant
    prefix (the part before the first ``/``).
    """

    skip_attempted: bool = False
    retry_after_days: float | None = None
    doi_prefixes: tuple[str, ...] = ()
    limit: int | None = None

    @property
    def is_noop(self) -> bool:
        return not (
            self.skip_attempted
            or self.retry_after_days is not None
            or self.doi_prefixes
            or self.limit is not None
        )


def last_fetch_attempt_at(folder: Path) -> datetime | None:
    """Return when this workspace was last attempted, or ``None``.

    Reads the ``fetch_result`` sidecar's own timestamp and falls back to the
    file's modification time, so a sidecar written before the timestamp field
    existed still counts as an attempt.
    """
    path = folder / "source_records" / FETCH_RESULT_FILENAME
    if not path.is_file():
        return None
    record = read_json(path, {})
    payload = record.get("fetch_result") if isinstance(record, dict) else None
    stamp = ""
    if isinstance(payload, dict):
        stamp = str(payload.get("fetched_at") or payload.get("downloaded_at") or "").strip()
    if stamp:
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def select_fetch_candidates(
    candidates: list[FetchCandidateStatus],
    selection: FetchSelection,
    *,
    now: datetime | None = None,
) -> list[FetchCandidateStatus]:
    """Return *candidates* with deselected ones marked ``skipped`` in place.

    Only workspaces that are already eligible are considered; anything else
    keeps the status ``classify_pdf_fetch_candidate`` gave it.  The returned
    list preserves input order so the caller's report stays aligned.
    """
    moment = now or datetime.now(timezone.utc)
    prefixes = tuple(prefix.strip().lower().rstrip("/") for prefix in selection.doi_prefixes if prefix.strip())
    taken = 0
    for candidate in candidates:
        if not candidate.eligible:
            continue
        if prefixes and candidate.doi.split("/", 1)[0].lower() not in prefixes:
            candidate.status = "skipped"
            candidate.reason = "DOI prefix not selected"
            continue
        attempted_at = (
            last_fetch_attempt_at(candidate.folder)
            if (selection.skip_attempted or selection.retry_after_days is not None)
            else None
        )
        if attempted_at is not None and selection.skip_attempted:
            candidate.status = "skipped"
            candidate.reason = "already attempted"
            continue
        if attempted_at is not None and selection.retry_after_days is not None:
            age_days = (moment - attempted_at).total_seconds() / 86400.0
            if age_days < selection.retry_after_days:
                candidate.status = "skipped"
                candidate.reason = f"attempted {age_days:.1f}d ago (< {selection.retry_after_days}d)"
                continue
        if selection.limit is not None and taken >= selection.limit:
            candidate.status = "skipped"
            candidate.reason = "beyond --limit for this batch"
            continue
        taken += 1
    return candidates


def classify_pdf_fetch_candidate(
    folder: Path,
    paper_number: str,
    *,
    force_refetch: bool = False,
) -> FetchCandidateStatus:
    paper_number = validate_paper_raw_id(paper_number)
    meta_path = folder / f"{paper_number}.metadata.json"
    pdf_path = folder / f"{paper_number}.pdf"
    status_data = read_json(folder / ".import_status.json", {})
    import_status = str(status_data.get("status") or "")

    item = FetchCandidateStatus(
        paper_number=paper_number,
        folder=folder,
        status="planned",
        has_metadata=meta_path.exists(),
        has_pdf=pdf_path.exists(),
        import_status=import_status,
    )
    if not folder.is_dir():
        item.status = "skipped"
        item.reason = "paper_raw folder missing"
        return item
    if "quarantine" in {part.lower() for part in folder.parts}:
        item.status = "skipped"
        item.reason = "workspace is under quarantine"
        return item
    if not (folder.name.isdigit() and len(folder.name) == 16):
        item.status = "skipped"
        item.reason = "not a 16-digit paper_raw workspace"
        return item
    if not meta_path.exists():
        item.status = "skipped"
        item.reason = "metadata file missing"
        return item
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        item.status = "failed"
        item.reason = f"metadata unreadable: {exc}"
        return item
    if not isinstance(metadata, dict):
        item.status = "failed"
        item.reason = "metadata must be a JSON object"
        return item
    item.metadata = metadata
    doi = normalize_doi(((metadata.get("identifiers") or {}).get("doi") or "").strip())
    item.doi = doi
    if not doi:
        item.status = "skipped"
        item.reason = "missing DOI in metadata"
        return item
    if not is_valid_normalized_doi(doi):
        item.status = "skipped"
        item.reason = "invalid DOI in metadata"
        return item
    if item.has_pdf and not force_refetch:
        item.status = "skipped"
        item.reason = "PDF already exists"
        return item
    if import_status in BLOCKED_FETCH_STATUSES:
        item.status = "skipped"
        item.reason = f"blocked import status: {import_status}"
        return item
    return item
