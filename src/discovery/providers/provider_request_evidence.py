"""Strict, credential-free evidence for provider requests.

The comparison corpus is intentionally offline-verifiable.  This module is
the single authority for the request-evidence wire format and for the two
hashes carried by an evidence record.  It does not know anything about
notebooks, ledgers, staging, or replay evaluation.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
from src.utils.canonical_json import canonical_json_bytes
from src.utils.timestamps import utc_now_iso as _now_iso


EVIDENCE_SCHEMA_VERSION = "1.1"

_COMMON_SIGNATURE_FIELDS = frozenset({
    "provider", "query", "lane", "sort", "page_size", "time_window",
    "pagination_schema_version",
})
_OPENALEX_SIGNATURE_FIELDS = frozenset({
    *_COMMON_SIGNATURE_FIELDS, "filter", "topic_filter",
})
_CROSSREF_SIGNATURE_FIELDS = frozenset({
    *_COMMON_SIGNATURE_FIELDS, "order",
})

# Kept public for callers that build a generic allowlist before selecting the
# provider-specific validator.
SAFE_SIGNATURE_ALLOWLIST: frozenset[str] = frozenset(
    _OPENALEX_SIGNATURE_FIELDS | _CROSSREF_SIGNATURE_FIELDS
)

_CREDENTIAL_KEYS = frozenset({
    "api_key", "apikey", "authorization", "cookie", "mailto", "proxy",
    "token", "access_token", "refresh_token", "client_secret", "password",
})

_EVIDENCE_FIELDS = frozenset({
    "schema_version", "budget_id", "request_sequence", "safe_signature",
    "cursor_in", "cursor_out", "response_hash", "request_timestamp",
    "observation_count", "semantic_hash", "observation_hash",
    "response_blob_sha256", "response_blob_path",
})
_HEX64 = set("0123456789abcdef")


class RequestEvidenceError(ValueError):
    """Raised when local evidence capture/validation fails.

    Provider adapters must let this error escape instead of turning a local
    evidence bug into a retryable provider-network failure.
    """



def _canonical_bytes(value: Any) -> bytes:
    return canonical_json_bytes(value)


def _is_hash(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and set(value) <= _HEX64
    )


def _is_timezone_aware_iso(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _normalise_time_window(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        parts = value.split("/", 1)
        return {"from": parts[0], "to": parts[1] if len(parts) == 2 else ""}
    if isinstance(value, Mapping):
        return {"from": str(value.get("from") or ""), "to": str(value.get("to") or "")}
    if value is None:
        return {"from": "", "to": ""}
    raise RequestEvidenceError("time_window must be a mapping, string, or null")


def validate_openalex_safe_signature(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return an exact OpenAlex safe-signature mapping."""
    return _validate_safe_signature(value, "openalex")


def validate_crossref_safe_signature(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return an exact Crossref safe-signature mapping."""
    return _validate_safe_signature(value, "crossref")


def _validate_safe_signature(value: Mapping[str, Any], provider: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RequestEvidenceError("safe_signature must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise RequestEvidenceError("safe_signature field names must be strings")
    expected = (
        _OPENALEX_SIGNATURE_FIELDS if provider == "openalex"
        else _CROSSREF_SIGNATURE_FIELDS if provider == "crossref"
        else frozenset()
    )
    if not expected:
        raise RequestEvidenceError(f"unsupported provider in safe_signature: {provider!r}")
    actual = set(value)
    unknown = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unknown:
        raise RequestEvidenceError(
            f"{provider} safe_signature has unknown fields: {unknown}"
        )
    if missing:
        raise RequestEvidenceError(
            f"{provider} safe_signature is missing fields: {missing}"
        )
    result = {str(key): value[key] for key in value}
    if result.get("provider") != provider:
        raise RequestEvidenceError(
            f"safe_signature provider must be {provider!r}"
        )
    for key in ("query", "lane", "sort", "pagination_schema_version"):
        if not isinstance(result[key], str):
            raise RequestEvidenceError(f"safe_signature.{key} must be a string")
    if result["lane"] not in {"refresh", "backfill"}:
        raise RequestEvidenceError("safe_signature.lane must be refresh or backfill")
    if type(result["page_size"]) is not int or result["page_size"] <= 0:
        raise RequestEvidenceError("safe_signature.page_size must be a positive integer")
    if not isinstance(result["time_window"], Mapping):
        raise RequestEvidenceError("safe_signature.time_window must be a mapping")
    time_window = dict(result["time_window"])
    if set(time_window) != {"from", "to"}:
        raise RequestEvidenceError("safe_signature.time_window fields must be exactly from/to")
    if not all(isinstance(time_window[key], str) for key in ("from", "to")):
        raise RequestEvidenceError("safe_signature.time_window values must be strings")
    if provider == "crossref" and (
        not isinstance(result["order"], str)
        or result["order"] not in {"", "asc", "desc"}
    ):
        raise RequestEvidenceError("Crossref safe_signature.order must be asc, desc, or empty")
    if provider == "openalex":
        for key in ("filter", "topic_filter"):
            if not isinstance(result[key], str):
                raise RequestEvidenceError(f"safe_signature.{key} must be a string")
    return {**result, "time_window": time_window}


def build_safe_signature(**kwargs: Any) -> dict[str, Any]:
    """Build a deterministic provider-specific signature.

    Unknown keyword arguments are rejected.  Provider adapters pass every
    schema field explicitly, including empty strings and an empty time window,
    so omitted fields cannot silently change request identity.
    """
    unknown = sorted(set(kwargs) - SAFE_SIGNATURE_ALLOWLIST)
    if unknown:
        raise RequestEvidenceError(f"unknown safe-signature fields: {unknown}")
    provider = str(kwargs.get("provider") or "")
    if provider == "openalex":
        fields = _OPENALEX_SIGNATURE_FIELDS
    elif provider == "crossref":
        fields = _CROSSREF_SIGNATURE_FIELDS
    else:
        # Preserve a useful error for callers that forgot provider rather than
        # producing a partial signature.
        raise RequestEvidenceError(f"unsupported provider: {provider!r}")
    result: dict[str, Any] = {}
    for field_name in sorted(fields):
        if field_name == "time_window":
            result[field_name] = _normalise_time_window(kwargs.get(field_name))
        elif field_name == "page_size":
            result[field_name] = kwargs.get(field_name, 0)
        else:
            value = kwargs.get(field_name, "")
            result[field_name] = value if value is not None else ""
    # Wire consumers call the provider-specific validators.  Keeping the
    # builder permissive lets lightweight callers construct an intermediate
    # signature for inspection; no such value can pass ``from_dict``.
    return result


def request_semantic_hash(safe_signature: Mapping[str, Any]) -> str:
    """Deterministic hash of the canonical request identity."""
    provider = str(safe_signature.get("provider") or "")
    normalized = (
        validate_openalex_safe_signature(safe_signature)
        if provider == "openalex"
        else validate_crossref_safe_signature(safe_signature)
    )
    return hashlib.sha256(_canonical_bytes(normalized)).hexdigest()



def safe_response_hash(response_bytes: bytes) -> str:
    """Content hash of the raw provider response body."""
    return hashlib.sha256(response_bytes).hexdigest()


def _compute_observation_hash(
    *, safe_signature: Mapping[str, Any], response_hash: str,
    cursor_in: str | None, cursor_out: str | None, request_sequence: int,
    request_timestamp: str, observation_count: int,
) -> str:
    payload = {
        "safe_signature": dict(safe_signature),
        "response_hash": response_hash,
        "cursor_in": cursor_in,
        "cursor_out": cursor_out,
        "request_sequence": request_sequence,
        "request_timestamp": request_timestamp,
        "observation_count": observation_count,
    }
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


@dataclass(frozen=True)
class ActualRequestEvidence:
    """One strict request observation.

    ``response_bytes`` is an in-memory capture detail and is never serialized;
    the corpus publisher stores it under the content-addressed response blob
    path and serializes only the hash/path facts.
    """

    safe_signature: Mapping[str, Any]
    cursor_in: str | None
    cursor_out: str | None
    response_hash: str
    request_timestamp: str = field(default_factory=_now_iso)
    observation_count: int = 0
    budget_id: str = ""
    request_sequence: int = 1
    semantic_hash: str = ""
    observation_hash: str = ""
    response_blob_sha256: str = ""
    response_blob_path: str = ""
    response_bytes: bytes = field(default=b"", repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.response_blob_sha256 and self.response_hash:
            object.__setattr__(self, "response_blob_sha256", self.response_hash)
        # Direct construction remains usable for lightweight provider tests;
        # strict wire validation is performed by ``from_dict``.
        if not self.semantic_hash:
            try:
                value = request_semantic_hash(self.safe_signature)
            except RequestEvidenceError:
                value = ""
            object.__setattr__(self, "semantic_hash", value)
        if not self.observation_hash:
            try:
                value = _compute_observation_hash(
                    safe_signature=self.safe_signature,
                    response_hash=self.response_hash,
                    cursor_in=self.cursor_in,
                    cursor_out=self.cursor_out,
                    request_sequence=self.request_sequence,
                    request_timestamp=self.request_timestamp,
                    observation_count=self.observation_count,
                )
            except Exception:
                value = ""
            object.__setattr__(self, "observation_hash", value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "budget_id": self.budget_id,
            "request_sequence": self.request_sequence,
            "safe_signature": dict(self.safe_signature),
            "cursor_in": self.cursor_in,
            "cursor_out": self.cursor_out,
            "response_hash": self.response_hash,
            "request_timestamp": self.request_timestamp,
            "observation_count": self.observation_count,
            "semantic_hash": self.semantic_hash,
            "observation_hash": self.observation_hash,
            "response_blob_sha256": self.response_blob_sha256,
            "response_blob_path": self.response_blob_path,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ActualRequestEvidence":
        if not isinstance(value, Mapping):
            raise RequestEvidenceError("request evidence must be a mapping")
        if any(not isinstance(key, str) for key in value):
            raise RequestEvidenceError("request evidence field names must be strings")
        unknown = sorted(set(value) - _EVIDENCE_FIELDS)
        missing = sorted(_EVIDENCE_FIELDS - set(value))
        if unknown:
            raise RequestEvidenceError(f"request evidence has unknown fields: {unknown}")
        if missing:
            raise RequestEvidenceError(f"request evidence is missing fields: {missing}")
        if value["schema_version"] != EVIDENCE_SCHEMA_VERSION:
            raise RequestEvidenceError(
                f"unsupported request evidence schema: {value['schema_version']!r}"
            )
        budget_id = value["budget_id"]
        if not isinstance(budget_id, str) or not budget_id.strip():
            raise RequestEvidenceError("budget_id must be a non-empty string")
        sequence = value["request_sequence"]
        if type(sequence) is not int or sequence < 1:
            raise RequestEvidenceError("request_sequence must be a positive integer")
        cursor_in = value["cursor_in"]
        cursor_out = value["cursor_out"]
        if cursor_in is not None and not isinstance(cursor_in, str):
            raise RequestEvidenceError("cursor_in must be a string or null")
        if cursor_out is not None and not isinstance(cursor_out, str):
            raise RequestEvidenceError("cursor_out must be a string or null")
        response_hash = value["response_hash"]
        if not _is_hash(response_hash):
            raise RequestEvidenceError("response_hash must be a lowercase SHA-256 hex string")
        observation_count = value["observation_count"]
        if type(observation_count) is not int or observation_count < 0:
            raise RequestEvidenceError("observation_count must be a non-negative integer")
        if not _is_timezone_aware_iso(value["request_timestamp"]):
            raise RequestEvidenceError("request_timestamp must be timezone-aware ISO-8601")
        safe_signature = value["safe_signature"]
        if not isinstance(safe_signature, Mapping):
            raise RequestEvidenceError("safe_signature must be a mapping")
        provider = safe_signature.get("provider")
        normalized_signature = (
            validate_openalex_safe_signature(safe_signature)
            if provider == "openalex"
            else validate_crossref_safe_signature(safe_signature)
        )
        semantic_hash = value["semantic_hash"]
        observation_hash = value["observation_hash"]
        if not _is_hash(semantic_hash) or not _is_hash(observation_hash):
            raise RequestEvidenceError("semantic_hash and observation_hash must be lowercase SHA-256 hex")
        blob_hash = value["response_blob_sha256"]
        if not _is_hash(blob_hash) or blob_hash != response_hash:
            raise RequestEvidenceError("response_blob_sha256 must equal response_hash")
        blob_path = value["response_blob_path"]
        if not isinstance(blob_path, str) or not blob_path:
            raise RequestEvidenceError("response_blob_path must be a non-empty POSIX-relative path")
        if "\\" in blob_path or blob_path.startswith("/") or any(
            part in {"", ".", ".."} for part in blob_path.split("/")
        ):
            raise RequestEvidenceError("response_blob_path must be a normalized POSIX-relative path")
        expected_blob_path = f"blobs/provider_response/{response_hash}.json"
        if blob_path != expected_blob_path:
            raise RequestEvidenceError(
                "response_blob_path must be the content-addressed provider response path"
            )
        computed_semantic = hashlib.sha256(_canonical_bytes(normalized_signature)).hexdigest()
        computed_observation = _compute_observation_hash(
            safe_signature=normalized_signature,
            response_hash=response_hash,
            cursor_in=cursor_in,
            cursor_out=cursor_out,
            request_sequence=sequence,
            request_timestamp=value["request_timestamp"],
            observation_count=observation_count,
        )
        if semantic_hash != computed_semantic:
            raise RequestEvidenceError("semantic_hash mismatch")
        if observation_hash != computed_observation:
            raise RequestEvidenceError("observation_hash mismatch")
        return cls(
            safe_signature=normalized_signature,
            cursor_in=cursor_in,
            cursor_out=cursor_out,
            response_hash=response_hash,
            request_timestamp=value["request_timestamp"],
            observation_count=observation_count,
            budget_id=budget_id,
            request_sequence=sequence,
            semantic_hash=semantic_hash,
            observation_hash=observation_hash,
            response_blob_sha256=blob_hash,
            response_blob_path=blob_path,
        )


def scan_safe_signature_for_credentials(value: Any) -> list[str]:
    """Recursively find credential-bearing keys in mappings and sequences."""
    found: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                normalized_key = str(key).casefold().replace("-", "_")
                if normalized_key in _CREDENTIAL_KEYS:
                    found.add(str(key))
                visit(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                visit(child)

    visit(value)
    return sorted(found)
