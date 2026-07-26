"""Serializable fetch result models."""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.utils.identifiers import normalize_doi


@dataclass
class FetchResult:
    doi: str
    success: bool = False
    source: str = ""
    pdf_url: str = ""
    output_path: str = ""
    sidecar_path: str = ""
    oa_status: str = ""
    license: str = ""
    sha256: str = ""
    error: str = ""
    downloaded_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)
    # 向后兼容字段：日志 / UI / 导入器使用
    status_code: int | None = None
    open_access: bool | None = None
    fetched_at: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    # Rich per-attempt report fields
    candidate_url: str = ""
    final_url: str = ""
    content_type: str = ""
    # PDF 获取架构字段
    access_mode: str = "oa_only"
    resolver: str = ""
    resolver_chain: list[str] = field(default_factory=list)
    landing_url: str = ""
    is_direct_pdf: bool | None = None
    requires_user_action: bool = False
    action_hint: str = ""
    access_status: str = ""
    # 补充材料
    supplementary_urls: list[str] = field(default_factory=list)
    supplementary_paths: list[str] = field(default_factory=list)
    has_supplementary: bool | None = None
    # 逐 resolver 尝试记录：每个 dict 含 resolver/status/reason/pdf_url/landing_url
    attempts: list[dict[str, Any]] = field(default_factory=list)
    # Sanitized HTTP transport records. Internal TransportAttempt objects are
    # serialized before they enter FetchResult; resolvers must not persist
    # headers, cookies, proxy URLs, or credentials here.
    transport_attempts: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.doi = normalize_doi(self.doi)
        # fetched_at 与 downloaded_at 同步：两者都空则取当前时间，否则任一为空用另一补齐
        if not self.fetched_at and not self.downloaded_at:
            now = datetime.now(timezone.utc).isoformat()
            self.fetched_at = now
            self.downloaded_at = now
        elif not self.fetched_at:
            self.fetched_at = self.downloaded_at
        elif not self.downloaded_at:
            self.downloaded_at = self.fetched_at
        # raw 与 metadata 互为兼容来源：任一为空则用另一补齐
        if not self.raw and self.metadata:
            self.raw = dict(self.metadata)
        if not self.metadata and self.raw:
            self.metadata = dict(self.raw)
        # access_mode 从 metadata 回退（默认 "oa_only" 时也回退）
        fallback_mode = (self.metadata or {}).get("access_mode", "")
        if fallback_mode and self.access_mode in ("", "oa_only"):
            self.access_mode = fallback_mode
        # resolver_chain 从 metadata 回退
        if not self.resolver_chain and self.metadata:
            rc = self.metadata.get("resolver_chain")
            if rc:
                self.resolver_chain = list(rc)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FetchResult":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
