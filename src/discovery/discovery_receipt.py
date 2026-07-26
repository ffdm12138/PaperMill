"""Shared discovery receipt writer with conflict validation.

This is the single entry point for writing discovery receipts anywhere in the
project. ``DiscoveryStageTransaction`` delegates here so receipt semantics are
identical across all transaction replays:

- Receipt absent  → atomic create + re-read verification → ``created``
- Receipt present, identity matches → no rewrite → ``existing_match`` (idempotent)
- Receipt present, identity conflicts → never overwrite, never delete →
  ``DiscoveryReceiptConflictError``

Receipt identity is defined by ``RECEIPT_IDENTITY_KEYS``. Mutable fields such as
``staged_at`` are intentionally excluded from the comparison so that a replay of
the same staging event is idempotent.

Workspace reconciliation lives in :mod:`src.discovery.stage_transaction`.
Every receipt write must go through
:func:`write_or_validate_discovery_receipt`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from filelock import FileLock

from src.discovery.models import normalize_doi
from src.services.ingest_ids import PAPER_NUMBER_RE
from src.utils.atomic_io import atomic_write_json_unlocked, lock_path_for


RECEIPT_SCHEMA_VERSION = "1.0"

# Fields that define receipt identity. Two receipts with the same values for
# every key here are the same receipt; ``staged_at`` and other mutable fields
# are excluded so re-staging is idempotent.
RECEIPT_IDENTITY_KEYS = (
    "schema_version",
    "paper_number",
    "candidate_id",
    "page_id",
    "keyword_id",
    "normalized_doi",
    "provider",
)


class DiscoveryReceiptConflictError(RuntimeError):
    """Raised when an existing receipt's identity conflicts with the payload.

    The conflicting receipt is left untouched on disk. The error message
    carries only safe identity fields — never the full raw payload — so it can
    be logged or written to a report without leaking provider responses.
    """

    def __init__(
        self,
        path: Path,
        *,
        candidate_id: str,
        page_id: str,
        keyword_id: str,
        normalized_doi: str,
    ) -> None:
        self.path = path
        self.candidate_id = candidate_id
        self.page_id = page_id
        self.keyword_id = keyword_id
        self.normalized_doi = normalized_doi
        super().__init__(
            f"discovery receipt conflict at {path}: "
            f"candidate_id={candidate_id!r} page_id={page_id!r} "
            f"keyword_id={keyword_id!r} normalized_doi={normalized_doi!r}"
        )


@dataclass(frozen=True)
class ReceiptWriteResult:
    """Outcome of :func:`write_or_validate_discovery_receipt`.

    ``status`` is one of ``"created"``, ``"existing_match"``. A conflict is
    raised as :class:`DiscoveryReceiptConflictError` rather than returned.
    """

    status: str
    path: Path
    paper_number: str


@dataclass(frozen=True)
class ReceiptLookupIdentity:
    candidate_id: str
    page_id: str
    keyword_id: str
    provider: str | None
    normalized_doi: str | None


@dataclass(frozen=True)
class PersistedReceiptIdentity:
    candidate_id: str
    page_id: str
    keyword_id: str
    provider: str | None
    normalized_doi: str | None
    paper_number: str


def normalize_receipt_lookup_key(
    key: ReceiptLookupIdentity | Mapping[str, Any],
) -> dict[str, str]:
    """Normalize receipt lookup fields that are known before paper_number."""
    payload: Mapping[str, Any]
    if isinstance(key, ReceiptLookupIdentity):
        payload = {
            "candidate_id": key.candidate_id,
            "page_id": key.page_id,
            "keyword_id": key.keyword_id,
            "provider": key.provider,
            "normalized_doi": key.normalized_doi,
        }
    else:
        payload = key
    candidate_id = str(payload.get("candidate_id") or "").strip()
    page_id = str(payload.get("page_id") or "").strip()
    keyword_id = str(payload.get("keyword_id") or "").strip()
    provider = str(payload.get("provider") or "").strip().lower()
    normalized_doi = normalize_doi(payload.get("normalized_doi") or payload.get("doi") or "")
    if not candidate_id or not page_id:
        raise ValueError("discovery receipt lookup requires candidate_id and page_id")
    return {
        "candidate_id": candidate_id,
        "page_id": page_id,
        "keyword_id": keyword_id,
        "provider": provider,
        "normalized_doi": normalized_doi,
    }


def normalize_receipt_identity(payload: Mapping[str, Any]) -> dict[str, str]:
    """Normalize the identity-bearing fields of a receipt payload.

    DOI is normalized via the project-wide :func:`normalize_doi`; paper_number
    and the discovery-context IDs are stripped of surrounding whitespace;
    ``provider`` is lower-cased. Raises ``ValueError`` when a required identity
    field (``candidate_id``, ``page_id``, ``normalized_doi``) is missing, since
    a receipt without those cannot be matched or validated.
    """
    schema_version = str(payload.get("schema_version") or RECEIPT_SCHEMA_VERSION).strip()
    candidate_id = str(payload.get("candidate_id") or "").strip()
    page_id = str(payload.get("page_id") or "").strip()
    keyword_id = str(payload.get("keyword_id") or "").strip()
    normalized_doi = normalize_doi(payload.get("normalized_doi") or payload.get("doi") or "")
    paper_number = str(payload.get("paper_number") or "").strip()
    if not PAPER_NUMBER_RE.match(paper_number):
        raise ValueError(
            f"discovery receipt paper_number must be exactly 16 decimal digits, "
            f"got: {paper_number!r}"
        )
    provider = str(payload.get("provider") or "").strip().lower()
    if not candidate_id or not page_id or not normalized_doi:
        raise ValueError(
            "discovery receipt identity requires candidate_id, page_id, and normalized_doi"
        )
    return {
        "schema_version": schema_version,
        "candidate_id": candidate_id,
        "page_id": page_id,
        "keyword_id": keyword_id,
        "normalized_doi": normalized_doi,
        "paper_number": paper_number,
        "provider": provider,
    }


def build_receipt_payload(
    *,
    candidate_id: str,
    page_id: str,
    keyword_id: str,
    normalized_doi: str,
    paper_number: str,
    provider: str = "",
    staged_at: str | None = None,
) -> dict[str, Any]:
    """Build a normalized receipt payload ready for atomic writing.

    ``staged_at`` defaults to the project-wide ISO timestamp. ``provider`` is
    optional: when empty it is still part of the identity (as ``""``) so a
    later non-empty provider correctly reads as a distinct receipt.
    """
    from src.ingest.models import now_iso

    identity = normalize_receipt_identity(
        {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "candidate_id": candidate_id,
            "page_id": page_id,
            "keyword_id": keyword_id,
            "normalized_doi": normalized_doi,
            "paper_number": paper_number,
            "provider": provider,
        }
    )
    payload: dict[str, Any] = {
        "schema_version": identity["schema_version"],
        "candidate_id": identity["candidate_id"],
        "page_id": identity["page_id"],
        "keyword_id": identity["keyword_id"],
        "normalized_doi": identity["normalized_doi"],
        "paper_number": identity["paper_number"],
        "staged_at": staged_at or now_iso(),
    }
    if identity["provider"]:
        payload["provider"] = identity["provider"]
    return payload


def receipt_path_for(paper_raw_dir: Path, paper_number: str) -> Path:
    """Resolve the canonical receipt path for a workspace."""
    if not PAPER_NUMBER_RE.match(str(paper_number or "")):
        raise ValueError("receipt_workspace_invalid: paper_number must be 16 digits")
    return Path(paper_raw_dir) / paper_number / f"{paper_number}.discovery_receipt.json"


def _validate_persisted_receipt_path(
    path: Path,
    paper_number: str,
    *,
    workspace_root: Path | None,
) -> None:
    """Bind payload identity to the canonical workspace and filename."""
    expected_name = f"{paper_number}.discovery_receipt.json"
    if path.name != expected_name:
        raise ValueError("receipt_filename_invalid: receipt filename must match payload paper_number")
    if not PAPER_NUMBER_RE.match(path.parent.name):
        raise ValueError("receipt_workspace_invalid: receipt parent must be a 16-digit workspace")
    if path.parent.name != paper_number:
        raise ValueError("receipt_paper_number_path_mismatch: payload and workspace differ")
    if workspace_root is not None:
        root = Path(workspace_root).resolve(strict=False)
        expected = (root / paper_number / expected_name).resolve(strict=False)
        actual = path.resolve(strict=False)
        if actual != expected:
            raise ValueError("receipt_workspace_invalid: receipt path escapes or aliases workspace root")


def write_or_validate_discovery_receipt(
    receipt_path: Path,
    receipt_payload: Mapping[str, Any],
    *,
    workspace_root: Path | None = None,
    fsync: bool = True,
) -> ReceiptWriteResult:
    """Write a discovery receipt atomically, or validate an existing one.

    See module docstring for the three outcomes. The **entire**
    read-check-write cycle runs inside a single ``FileLock`` so that two
    concurrent writers can never both see "file absent" and silently
    overwrite each other (TOCTOU). The receipt is written via
    :func:`atomic_write_json_unlocked` (tmp + ``os.replace`` + fsync)
    and then re-read to verify that the identity survived the round trip.
    """
    path = Path(receipt_path)
    identity = normalize_receipt_identity(receipt_payload)
    paper_number = identity["paper_number"]
    _validate_persisted_receipt_path(
        path, paper_number, workspace_root=workspace_root
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_path_for(path)))
    with lock:
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise ValueError(f"discovery receipt is corrupt: {path}") from exc
            existing_identity = normalize_receipt_identity(existing)
            if existing_identity != identity:
                raise DiscoveryReceiptConflictError(
                    path,
                    candidate_id=identity["candidate_id"],
                    page_id=identity["page_id"],
                    keyword_id=identity["keyword_id"],
                    normalized_doi=identity["normalized_doi"],
                )
            return ReceiptWriteResult(
                status="existing_match", path=path, paper_number=paper_number
            )
        # File does not exist — we hold the lock, so no race.
        atomic_write_json_unlocked(path, dict(receipt_payload), indent=2, fsync=fsync)
        try:
            verify = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(
                f"discovery receipt verification read failed: {path}"
            ) from exc
        if normalize_receipt_identity(verify) != identity:
            raise ValueError(f"discovery receipt verification mismatch: {path}")
    return ReceiptWriteResult(status="created", path=path, paper_number=paper_number)
