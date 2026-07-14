"""Durable provider-page journal for DOI discovery.

The journal is the safety boundary between provider pagination and candidate
processing. Backfill cursors may advance only after a provider page is persisted
here. Candidate state also lives here so ``max_candidates`` can stop a run
without losing unprocessed observations.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from filelock import FileLock

from src.discovery.keyword_notebook import (
    PROVIDERS,
    detect_query_language,
    keyword_id as make_keyword_id,
    normalize_keyword,
    query_identity,
)
from src.discovery.models import PaperCandidate, normalize_doi, normalize_title


from src.discovery.constants import INITIAL_CURSOR


PAGE_SCHEMA_VERSION = "2.0"

# ── Exact field set for v2 page journals ───────────────────────────
# ALL_V2_FIELDS is used to reject unknown fields.
# REQUIRED_V2_FIELDS (a subset) is used to reject missing critical fields.
PAGE_V2_FIELDS: frozenset[str] = frozenset({
    "schema_version", "page_id", "keyword_id", "keyword_zh",
    "query_id", "query", "query_language", "provider", "lane",
    "generation", "request_signature",
    "request_cursor", "next_cursor",
    "provider_exhausted", "state",
    "fetched_at", "cursor_committed_at", "drained_at",
    "candidates", "statistics",
    "refresh_run_id", "page_sequence",
})

# Backwards-compatible aliases used by the v3 migration module.
PAGE_ALL_V2_FIELDS = PAGE_V2_FIELDS
PAGE_REQUIRED_V2_FIELDS = PAGE_V2_FIELDS

PageLane = Literal["refresh", "backfill"]
PageState = Literal["fetched", "cursor_committed", "draining", "drained", "failed"]
CandidateState = Literal[
    "pending",
    "resolution_pending",
    "ready",
    "processing",
    "staged",
    "emitted",
    "existing_duplicate",
    "duplicate_observation",
    "invalid_doi",
    "unresolved",
    "failed_retryable",
    "failed_terminal",
]

TERMINAL_CANDIDATE_STATES = {
    "staged",
    "emitted",
    "existing_duplicate",
    "duplicate_observation",
    "invalid_doi",
    "unresolved",
    "failed_terminal",
}
NONTERMINAL_CANDIDATE_STATES = {
    "pending",
    "resolution_pending",
    "ready",
    "processing",
    "failed_retryable",
}

_PAGE_TRANSITIONS = {
    "fetched": {"cursor_committed", "failed"},
    "cursor_committed": {"draining", "drained"},
    "draining": {"cursor_committed", "drained"},
    "drained": set(),
    "failed": set(),
}

_CANDIDATE_TRANSITIONS = {
    "pending": {
        "resolution_pending",
        "ready",
        "processing",
        "existing_duplicate",
        "duplicate_observation",
        "invalid_doi",
        "unresolved",
        "failed_terminal",
    },
    "resolution_pending": {
        "ready",
        "processing",
        "duplicate_observation",
        "unresolved",
        "failed_retryable",
        "failed_terminal",
    },
    "ready": {
        "processing",
        "existing_duplicate",
        "duplicate_observation",
        "invalid_doi",
        "failed_terminal",
    },
    "processing": {
        "staged",
        "emitted",
        "existing_duplicate",
        "duplicate_observation",
        "invalid_doi",
        "unresolved",
        "failed_retryable",
        "failed_terminal",
    },
    "failed_retryable": {"processing"},
    "staged": set(),
    "emitted": set(),
    "existing_duplicate": set(),
    "duplicate_observation": set(),
    "invalid_doi": set(),
    "unresolved": set(),
    "failed_terminal": set(),
}


class JournalCorruptError(RuntimeError):
    """Raised when a page journal is missing required structure or bad JSON."""


class InvalidStateTransition(RuntimeError):
    """Raised when code attempts a forbidden page/candidate transition."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def stable_hash(*parts: Any, length: int = 32) -> str:
    payload = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def request_signature(
    *,
    sort: str | None = None,
    filters: dict[str, Any] | None = None,
    page_size: int,
    pagination_schema_version: str = "2.0",
) -> dict[str, Any]:
    return {
        "sort": sort or "",
        "filters": filters or {},
        "page_size": int(page_size),
        "pagination_schema_version": pagination_schema_version,
        "hash": stable_hash(
            sort or "",
            json.dumps(filters or {}, ensure_ascii=False, sort_keys=True),
            int(page_size),
            pagination_schema_version,
            length=16,
        ),
    }


def backfill_page_id(
    *,
    keyword_id: str,
    query_id: str,
    provider: str,
    request_signature_hash: str,
    request_cursor: str | None,
) -> str:
    return stable_hash(
        "backfill",
        keyword_id,
        query_id,
        provider,
        request_signature_hash,
        request_cursor or INITIAL_CURSOR,
    )


def refresh_page_id(
    *,
    keyword_id: str,
    query_id: str,
    provider: str,
    request_signature_hash: str,
    refresh_run_id: str,
    page_sequence: int,
) -> str:
    return stable_hash(
        "refresh",
        keyword_id,
        query_id,
        provider,
        request_signature_hash,
        refresh_run_id,
        int(page_sequence),
    )


def provider_record_id(candidate: PaperCandidate) -> str:
    raw = candidate.raw if isinstance(candidate.raw, dict) else {}
    for key in ("id", "openalex_id", "crossref_id", "DOI", "doi"):
        value = raw.get(key)
        if value:
            return str(value)
    return str(candidate.source_id or "")


def candidate_id(page_id: str, candidate: PaperCandidate, page_item_index: int) -> str:
    rid = provider_record_id(candidate)
    if rid:
        return stable_hash(page_id, rid)
    doi = normalize_doi(candidate.doi)
    if doi:
        return stable_hash(page_id, doi)
    return stable_hash(page_id, normalize_title(candidate.title), int(page_item_index))


def title_resolution_key(candidate: dict[str, Any] | PaperCandidate) -> str:
    if isinstance(candidate, PaperCandidate):
        title = candidate.title
        year = candidate.year
        authors = candidate.authors
    else:
        payload = candidate.get("candidate") if isinstance(candidate.get("candidate"), dict) else candidate
        title = str(payload.get("title") or "")
        year = payload.get("year")
        authors = payload.get("authors") or []
    first_author = ""
    if isinstance(authors, list) and authors:
        first = authors[0]
        first_author = str(first.get("full_name") or first.get("name") if isinstance(first, dict) else first)
    return stable_hash("resolution", normalize_title(title), year or "", first_author.lower(), length=40)


def _candidate_record(page_id_value: str, candidate: PaperCandidate, index: int) -> dict[str, Any]:
    cid = candidate_id(page_id_value, candidate, index)
    payload = candidate.to_dict()
    return {
        "candidate_id": cid,
        "candidate": payload,
        "status": "pending",
        "attempts": 0,
        "last_error": None,
        "terminal_reason": None,
        "staged_paper_number": None,
        "claimed_by": None,
        "claimed_at": None,
        "lease_expires_at": None,
        "export_id": None,
        "export_path": None,
        "emitted_at": None,
        "reconciled": False,
    }


def _atomic_write_json_unlocked(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically (caller holds lock) with fsync durability.

    Delegates to :func:`src.utils.atomic_io.atomic_write_json_unlocked`
    so that all durable writers share the same fsync + tmp + os.replace
    + parent-dir-fsync implementation.
    """
    from src.utils.atomic_io import atomic_write_json_unlocked as _unlocked

    _unlocked(path, data, indent=2)


def validate_page(data: Any, path: Path | None = None) -> dict[str, Any]:
    """Strictly validate one active schema-v2 provider-page journal."""
    if not isinstance(data, dict):
        raise JournalCorruptError(f"journal root is not object: {path or ''}")
    missing = sorted(PAGE_V2_FIELDS - set(data))
    if missing:
        raise JournalCorruptError(f"journal missing keys {missing}: {path or ''}")
    unexpected = sorted(set(data) - PAGE_V2_FIELDS)
    if unexpected:
        raise JournalCorruptError(f"journal contains unexpected fields {unexpected}: {path or ''}")
    if data.get("schema_version") != PAGE_SCHEMA_VERSION:
        raise JournalCorruptError(f"journal schema_version must be {PAGE_SCHEMA_VERSION}: {path or ''}")
    if not isinstance(data.get("page_id"), str) or not data["page_id"]:
        raise JournalCorruptError(f"journal page_id must be non-blank: {path or ''}")
    keyword_zh = data.get("keyword_zh")
    if (
        not isinstance(keyword_zh, str)
        or not keyword_zh.strip()
        or detect_query_language(keyword_zh) not in {"zh", "mixed"}
    ):
        raise JournalCorruptError(f"journal keyword_zh must be non-blank: {path or ''}")
    if data.get("keyword_id") != make_keyword_id(keyword_zh):
        raise JournalCorruptError(f"journal keyword_id does not match keyword_zh: {path or ''}")
    query = data.get("query")
    language = data.get("query_language")
    if not isinstance(query, str) or not query.strip():
        raise JournalCorruptError(f"journal query must be non-blank: {path or ''}")
    if language not in {"zh", "en"} or detect_query_language(query) != language:
        raise JournalCorruptError(f"journal query_language is invalid: {path or ''}")
    expected_query_id = query_identity(language, normalize_keyword(query))
    if data.get("query_id") != expected_query_id:
        raise JournalCorruptError(f"journal query_id does not match query: {path or ''}")
    if data.get("provider") not in PROVIDERS:
        raise JournalCorruptError(f"invalid provider: {data.get('provider')}: {path or ''}")
    if data.get("lane") not in {"refresh", "backfill"}:
        raise JournalCorruptError(f"invalid lane: {data.get('lane')}: {path or ''}")
    generation = data.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise JournalCorruptError(f"journal generation must be a positive integer: {path or ''}")
    if data.get("state") not in _PAGE_TRANSITIONS:
        raise JournalCorruptError(f"invalid page state: {data.get('state')}: {path or ''}")
    signature = data.get("request_signature")
    if not isinstance(signature, dict):
        raise JournalCorruptError(f"journal request_signature must be object: {path or ''}")
    signature_required = {
        "sort", "filters", "page_size", "pagination_schema_version", "hash",
    }
    if not signature_required.issubset(signature):
        raise JournalCorruptError(f"journal request_signature is incomplete: {path or ''}")
    if not isinstance(signature.get("filters"), dict):
        raise JournalCorruptError(f"journal request_signature.filters must be object: {path or ''}")
    try:
        expected_signature = request_signature(
            sort=str(signature.get("sort") or ""),
            filters=signature.get("filters"),
            page_size=int(signature["page_size"]),
            pagination_schema_version=str(signature["pagination_schema_version"]),
        )
    except (TypeError, ValueError) as exc:
        raise JournalCorruptError(f"journal request_signature is invalid: {path or ''}") from exc
    if signature != expected_signature:
        raise JournalCorruptError(f"journal request_signature hash/content mismatch: {path or ''}")
    if data.get("request_cursor") is not None and not isinstance(data.get("request_cursor"), str):
        raise JournalCorruptError(f"journal request_cursor must be string or null: {path or ''}")
    if data.get("next_cursor") is not None and not isinstance(data.get("next_cursor"), str):
        raise JournalCorruptError(f"journal next_cursor must be string or null: {path or ''}")
    if not isinstance(data.get("provider_exhausted"), bool):
        raise JournalCorruptError(f"journal provider_exhausted must be boolean: {path or ''}")
    for field_name in ("fetched_at", "cursor_committed_at", "drained_at"):
        value = data.get(field_name)
        if value is not None and not isinstance(value, str):
            raise JournalCorruptError(f"journal {field_name} must be string or null: {path or ''}")
    if not isinstance(data.get("candidates"), list):
        raise JournalCorruptError(f"journal candidates must be list: {path or ''}")
    for item in data["candidates"]:
        if not isinstance(item, dict) or "candidate_id" not in item:
            raise JournalCorruptError(f"invalid candidate record: {path or ''}")
        status = item.get("status")
        if status not in _CANDIDATE_TRANSITIONS:
            raise JournalCorruptError(f"invalid candidate state {status}: {path or ''}")
    if not isinstance(data.get("statistics"), dict):
        raise JournalCorruptError(f"journal statistics must be object: {path or ''}")
    return data


_validate_page = validate_page


@dataclass(frozen=True)
class PageRef:
    path: Path
    page_id: str
    keyword_id: str
    query_id: str
    provider: str
    lane: str
    state: str
    fetched_at: str


@dataclass(frozen=True)
class ClaimResult:
    claimed: bool
    page_path: Path
    candidate_id: str
    candidate: dict[str, Any] | None = None
    reason: str = ""


class PageJournalStore:
    """File-backed page journal store.

    Lock order rule for candidate drain: acquire page locks only for short
    claim/commit mutations. Never wait for DOI, resolution, export, or
    ``paper_raw`` locks while holding a page lock.
    """

    def __init__(self, root_dir: str | Path):
        self.root_dir = Path(root_dir)

    def page_path(
        self,
        *,
        keyword_id: str,
        query_id: str,
        provider: str,
        lane: PageLane,
        page_id: str,
    ) -> Path:
        return self.root_dir / keyword_id / query_id / provider / lane / f"{page_id}.json"

    @staticmethod
    def lock_for(path: Path) -> FileLock:
        return FileLock(str(path.with_suffix(path.suffix + ".lock")))

    def read(self, path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise JournalCorruptError(f"journal JSON corrupt: {path}: {exc}") from exc
        return _validate_page(data, path)

    def write_page(self, page: dict[str, Any]) -> Path:
        page = validate_page(page)
        path = self.page_path(
            keyword_id=page["keyword_id"],
            query_id=page["query_id"],
            provider=page["provider"],
            lane=page["lane"],
            page_id=page["page_id"],
        )
        with self.lock_for(path):
            if path.exists():
                existing = self.read(path)
                if existing.get("request_signature") != page.get("request_signature"):
                    raise JournalCorruptError(f"page_id collision with different signature: {path}")
                return path
            _atomic_write_json_unlocked(path, _validate_page(page, path))
        return path

    def make_page(
        self,
        *,
        page_id: str,
        keyword_id: str,
        keyword_zh: str,
        query_id: str,
        query: str,
        query_language: str,
        provider: str,
        lane: PageLane,
        request_signature_value: dict[str, Any],
        request_cursor: str | None,
        next_cursor: str | None,
        provider_exhausted: bool,
        candidates: list[PaperCandidate],
        generation: int = 1,
        refresh_run_id: str | None = None,
        page_sequence: int | None = None,
        state: PageState = "fetched",
    ) -> dict[str, Any]:
        now = now_iso()
        records = [_candidate_record(page_id, cand, idx) for idx, cand in enumerate(candidates)]
        return {
            "schema_version": PAGE_SCHEMA_VERSION,
            "page_id": page_id,
            "keyword_id": keyword_id,
            "keyword_zh": keyword_zh,
            "query_id": query_id,
            "query": query,
            "query_language": query_language,
            "provider": provider,
            "lane": lane,
            "generation": int(generation),
            "refresh_run_id": refresh_run_id,
            "page_sequence": page_sequence,
            "request_signature": request_signature_value,
            "request_cursor": request_cursor,
            "next_cursor": next_cursor,
            "provider_exhausted": bool(provider_exhausted),
            "state": state,
            "fetched_at": now,
            "cursor_committed_at": now if state == "cursor_committed" else None,
            "drained_at": None,
            "candidates": records,
            "statistics": _statistics(records),
        }

    def transition_page(self, path: Path, new_state: PageState) -> dict[str, Any]:
        with self.lock_for(path):
            data = self.read(path)
            old = data["state"]
            if new_state not in _PAGE_TRANSITIONS[old]:
                raise InvalidStateTransition(f"page {old} -> {new_state} is not allowed")
            data["state"] = new_state
            if new_state == "cursor_committed":
                data["cursor_committed_at"] = now_iso()
            if new_state == "drained":
                data["drained_at"] = now_iso()
            data["statistics"] = _statistics(data["candidates"])
            _atomic_write_json_unlocked(path, data)
            return data

    def mark_cursor_committed(self, path: Path) -> dict[str, Any]:
        with self.lock_for(path):
            data = self.read(path)
            if data["state"] == "cursor_committed":
                return data
            if data["state"] != "fetched":
                raise InvalidStateTransition(f"cannot mark cursor_committed from {data['state']}")
            data["state"] = "cursor_committed"
            data["cursor_committed_at"] = now_iso()
            data["statistics"] = _statistics(data["candidates"])
            _atomic_write_json_unlocked(path, data)
            return data

    def list_pages(self, keyword_ids: Iterable[str] | None = None) -> list[PageRef]:
        if not self.root_dir.exists():
            return []
        wanted = set(keyword_ids or [])
        refs: list[PageRef] = []
        for path in sorted(self.root_dir.glob("*/*/*/*/*.json")):
            try:
                data = self.read(path)
            except JournalCorruptError:
                raise
            if wanted and data.get("keyword_id") not in wanted:
                continue
            refs.append(PageRef(
                path=path,
                page_id=data["page_id"],
                keyword_id=data["keyword_id"],
                query_id=data["query_id"],
                provider=data["provider"],
                lane=data["lane"],
                state=data["state"],
                fetched_at=data.get("fetched_at") or "",
            ))
        refs.sort(key=lambda r: (
            0 if r.state in {"cursor_committed", "draining"} else 1,
            0 if r.lane == "backfill" else 1,
            r.fetched_at,
            str(r.path),
        ))
        return refs

    def count_pending_candidates(self, keyword_ids: Iterable[str] | None = None) -> int:
        count = 0
        for ref in self.list_pages(keyword_ids):
            data = self.read(ref.path)
            for item in data["candidates"]:
                if item.get("status") in NONTERMINAL_CANDIDATE_STATES:
                    count += 1
        return count

    def claim_candidate(
        self,
        page_path: Path,
        *,
        candidate_id_value: str,
        worker_id: str,
        lease_seconds: int,
    ) -> ClaimResult:
        with self.lock_for(page_path):
            data = self.read(page_path)
            if data["state"] not in {"cursor_committed", "draining", "drained"}:
                return ClaimResult(
                    False,
                    page_path,
                    candidate_id_value,
                    reason=f"page_not_claimable:{data['state']}",
                )
            if data["state"] == "drained":
                return ClaimResult(False, page_path, candidate_id_value, reason="page_drained")
            if data["state"] == "cursor_committed":
                data["state"] = "draining"
            now_dt = datetime.now(timezone.utc)
            for item in data["candidates"]:
                if item.get("candidate_id") != candidate_id_value:
                    continue
                status = item.get("status")
                expires = parse_iso(item.get("lease_expires_at"))
                next_attempt = parse_iso(item.get("next_attempt_at"))
                if next_attempt and next_attempt > now_dt:
                    return ClaimResult(False, page_path, candidate_id_value, reason="deferred_until_next_attempt")
                if status == "processing" and expires and expires > now_dt:
                    return ClaimResult(False, page_path, candidate_id_value, reason="lease_active")
                if status not in {"pending", "ready", "failed_retryable", "processing"}:
                    return ClaimResult(False, page_path, candidate_id_value, reason=f"not_claimable:{status}")
                _transition_candidate(item, "processing")
                item["claimed_by"] = worker_id
                item["claimed_at"] = now_iso()
                item["lease_expires_at"] = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
                item["attempts"] = int(item.get("attempts") or 0) + 1
                data["statistics"] = _statistics(data["candidates"])
                _atomic_write_json_unlocked(page_path, data)
                return ClaimResult(True, page_path, candidate_id_value, candidate=dict(item))
        return ClaimResult(False, page_path, candidate_id_value, reason="candidate_not_found")

    def renew_candidate_lease(
        self,
        page_path: Path,
        *,
        candidate_id_value: str,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        with self.lock_for(page_path):
            data = self.read(page_path)
            for item in data["candidates"]:
                if item.get("candidate_id") == candidate_id_value:
                    if item.get("status") != "processing" or item.get("claimed_by") != worker_id:
                        return False
                    item["lease_expires_at"] = (
                        datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
                    ).isoformat()
                    _atomic_write_json_unlocked(page_path, data)
                    return True
        return False

    def defer_candidate(
        self,
        page_path: Path,
        *,
        candidate_id_value: str,
        worker_id: str,
        reason: str,
        drain_generation: str = "",
        next_attempt_at: str | None = None,
        updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Release a claimed candidate as retryable without making it terminal.

        Used when another observation only has a temporary DOI claim, or when a
        formal workspace needs repair outside the discovery drain loop. The
        claim owner check mirrors ``commit_candidate`` so an unrelated worker
        cannot steal or release another active lease.
        """
        with self.lock_for(page_path):
            data = self.read(page_path)
            for item in data["candidates"]:
                if item.get("candidate_id") != candidate_id_value:
                    continue
                if item.get("status") != "processing" or item.get("claimed_by") != worker_id:
                    raise InvalidStateTransition("only claim owner may defer processing candidate")
                _transition_candidate(item, "failed_retryable")
                item["claimed_by"] = None
                item["claimed_at"] = None
                item["lease_expires_at"] = None
                item["last_deferred_reason"] = reason
                if drain_generation:
                    item["deferred_generation"] = drain_generation
                if next_attempt_at:
                    item["next_attempt_at"] = next_attempt_at
                elif "next_attempt_at" in item:
                    item.pop("next_attempt_at", None)
                if updates:
                    item.update(updates)
                data["statistics"] = _statistics(data["candidates"])
                _atomic_write_json_unlocked(page_path, data)
                return dict(item)
        raise KeyError(f"candidate not found: {candidate_id_value}")

    def commit_candidate(
        self,
        page_path: Path,
        *,
        candidate_id_value: str,
        worker_id: str,
        new_status: CandidateState,
        updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.lock_for(page_path):
            data = self.read(page_path)
            for item in data["candidates"]:
                if item.get("candidate_id") != candidate_id_value:
                    continue
                if item.get("status") == "processing" and item.get("claimed_by") != worker_id:
                    raise InvalidStateTransition("only claim owner may commit processing result")
                _transition_candidate(item, new_status)
                if updates:
                    item.update(updates)
                if new_status in TERMINAL_CANDIDATE_STATES or new_status == "failed_retryable":
                    item["claimed_by"] = None
                    item["claimed_at"] = None
                    item["lease_expires_at"] = None
                data["statistics"] = _statistics(data["candidates"])
                if _all_terminal(data["candidates"]):
                    if data["state"] != "drained":
                        if data["state"] not in {"cursor_committed", "draining"}:
                            raise InvalidStateTransition(f"cannot drain page from {data['state']}")
                        data["state"] = "drained"
                        data["drained_at"] = now_iso()
                _atomic_write_json_unlocked(page_path, data)
                return dict(item)
        raise KeyError(f"candidate not found: {candidate_id_value}")

    def update_candidate_payload(
        self,
        page_path: Path,
        *,
        candidate_id_value: str,
        worker_id: str,
        candidate_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Update a claimed candidate payload without changing its identity/state.

        Title resolution can enrich a no-DOI observation after the candidate has
        been claimed. The candidate_id is intentionally stable: receipts,
        leases, and recovery records all key off the original observation.
        """
        with self.lock_for(page_path):
            data = self.read(page_path)
            for item in data["candidates"]:
                if item.get("candidate_id") != candidate_id_value:
                    continue
                if item.get("status") != "processing" or item.get("claimed_by") != worker_id:
                    raise InvalidStateTransition("only claim owner may update candidate payload")
                item["candidate"] = dict(candidate_payload)
                _validate_page(data, page_path)
                _atomic_write_json_unlocked(page_path, data)
                return dict(item)
        raise KeyError(f"candidate not found: {candidate_id_value}")

    def iter_claimable(self, keyword_ids: Iterable[str] | None = None) -> list[tuple[Path, dict[str, Any]]]:
        now_dt = datetime.now(timezone.utc)
        out: list[tuple[Path, dict[str, Any]]] = []
        for ref in self.list_pages(keyword_ids):
            if ref.state not in {"cursor_committed", "draining"}:
                continue
            data = self.read(ref.path)
            for item in data["candidates"]:
                status = item.get("status")
                expires = parse_iso(item.get("lease_expires_at"))
                next_attempt = parse_iso(item.get("next_attempt_at"))
                if next_attempt and next_attempt > now_dt:
                    continue
                if status in {"pending", "ready", "failed_retryable"}:
                    out.append((ref.path, dict(item)))
                elif status == "processing" and (not expires or expires <= now_dt):
                    out.append((ref.path, dict(item)))
        return out


def _transition_candidate(item: dict[str, Any], new_state: CandidateState) -> None:
    old = item.get("status")
    if new_state not in _CANDIDATE_TRANSITIONS.get(old, set()):
        if old == new_state:
            return
        raise InvalidStateTransition(f"candidate {old} -> {new_state} is not allowed")
    item["status"] = new_state


def _all_terminal(candidates: list[dict[str, Any]]) -> bool:
    return all(item.get("status") in TERMINAL_CANDIDATE_STATES for item in candidates)


def _statistics(candidates: list[dict[str, Any]]) -> dict[str, int]:
    stats = {
        "returned": len(candidates),
        "pending": 0,
        "terminal": 0,
        "staged": 0,
        "emitted": 0,
        "existing_duplicate": 0,
        "duplicate_observation": 0,
        "invalid": 0,
        "unresolved": 0,
        "failed_retryable": 0,
        "failed_terminal": 0,
    }
    for item in candidates:
        status = str(item.get("status") or "")
        if status in TERMINAL_CANDIDATE_STATES:
            stats["terminal"] += 1
        else:
            stats["pending"] += 1
        if status in stats:
            stats[status] += 1
        if status == "invalid_doi":
            stats["invalid"] += 1
    return stats
