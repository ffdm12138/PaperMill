"""JournalDrainIndex — canonical self-contained v4 drain index.

Rebuildable in-memory index over page journals.  Page journals remain the
fact source; this index is a performance cache.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


class _IndexState:
    """Immutable snapshot of all index projections — swapped atomically."""
    __slots__ = (
        "candidate_by_id", "claimable_by_keyword", "processing_by_doi",
        "emitted_by_doi", "terminal_by_doi", "page_cache", "delayed_candidate_ids",
    )

    def __init__(
        self,
        candidate_by_id: dict[str, CandidateRef] | None = None,
        claimable_by_keyword: dict[str, deque[str]] | None = None,
        processing_by_doi: dict[str, str] | None = None,
        emitted_by_doi: dict[str, EmittedPrimaryRef] | None = None,
        terminal_by_doi: dict[str, list[str]] | None = None,
        page_cache: dict[Path, dict[str, Any]] | None = None,
        delayed_candidate_ids: set[str] | None = None,
    ):
        self.candidate_by_id: dict[str, CandidateRef] = candidate_by_id or {}
        self.claimable_by_keyword: dict[str, deque[str]] = claimable_by_keyword or {}
        self.processing_by_doi: dict[str, str] = processing_by_doi or {}
        self.emitted_by_doi: dict[str, EmittedPrimaryRef] = emitted_by_doi or {}
        self.terminal_by_doi: dict[str, list[str]] = terminal_by_doi or {}
        self.page_cache: dict[Path, dict[str, Any]] = page_cache or {}
        self.delayed_candidate_ids: set[str] = delayed_candidate_ids or set()

    def copy(self) -> "_IndexState":
        return _IndexState(
            candidate_by_id=dict(self.candidate_by_id),
            claimable_by_keyword={
                kw: deque(q) for kw, q in self.claimable_by_keyword.items()
            },
            processing_by_doi=dict(self.processing_by_doi),
            emitted_by_doi=dict(self.emitted_by_doi),
            terminal_by_doi={
                doi: list(cids) for doi, cids in self.terminal_by_doi.items()
            },
            page_cache=dict(self.page_cache),
            delayed_candidate_ids=set(self.delayed_candidate_ids),
        )


@dataclass
class JournalDrainIndex:
    """One full journal read per batch followed by in-memory lookups.

    All mutable projections are stored in a single ``_state``
    reference that is swapped atomically under ``_lock``.  External
    modules must use the reader accessor methods — never read fields
    directly.
    """
    _state: _IndexState = field(default_factory=_IndexState)
    dirty_pages: set[Path] = field(default_factory=set)
    emitted_validation_cache: dict[
        tuple[str, int, int, str, int, int], tuple[bool, str]
    ] = field(default_factory=dict)
    full_scans: int = 1
    pages_read: int = 0
    lookups: int = 0
    active_profile_hashes: dict[str, str] = field(default_factory=dict)
    relevance_updates: int = 0
    binding_invariant_failures: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @classmethod
    def build(
        cls,
        store: "PageJournalStoreV4",
        *,
        active_profile_hashes: Mapping[str, str],
    ) -> "JournalDrainIndex":
        bindings = {str(key): str(value) for key, value in active_profile_hashes.items()}
        if any(not key or not value for key, value in bindings.items()):
            raise ValueError("active relevance profile bindings must be non-blank")
        state = _IndexState()
        index = cls(
            _state=state, pages_read=0,
            active_profile_hashes=bindings,
        )
        if not store.root_dir.exists():
            return index
        now_dt = datetime.now(timezone.utc)
        cid_page_tracker: dict[str, Path] = {}
        for path in sorted(store.root_dir.glob("*/*/*/*/*.json")):
            page = store.read(path)
            index.pages_read += 1
            state.page_cache[path] = page
            keyword_id = str(page["keyword_id"])
            expected_profile_hash = bindings.get(keyword_id)
            queue = state.claimable_by_keyword.setdefault(keyword_id, deque())
            for item in page["candidates"]:
                cid = str(item.get("candidate_id") or "")
                if not cid:
                    continue
                # Cross-page candidate_id collision detection.
                existing_path = cid_page_tracker.get(cid)
                if existing_path is not None and existing_path != path:
                    raise JournalCorruptError(
                        f"candidate_id collision across pages: {cid} on "
                        f"{existing_path} and {path}"
                    )
                cid_page_tracker[cid] = path
                state.candidate_by_id[cid] = CandidateRef(cid, keyword_id, path, dict(item))
                doi = _candidate_doi(item)
                status = str(item.get("status") or "")
                expires = parse_iso(item.get("lease_expires_at"))
                next_attempt = parse_iso(item.get("next_attempt_at"))
                claimable = (not next_attempt or next_attempt <= now_dt) and (
                    status in {"pending", "ready", "failed_retryable"}
                    or status == "processing" and (not expires or expires <= now_dt))
                claimable = bool(expected_profile_hash) and claimable and _relevance_claimable(
                    item, expected_profile_hash,
                )
                drain_visible = page_is_drain_visible(page)
                if claimable and drain_visible:
                    queue.append(cid)
                elif (next_attempt and next_attempt > now_dt
                      and status in {"pending", "ready", "failed_retryable"}
                      and bool(expected_profile_hash)
                      and _relevance_claimable(item, expected_profile_hash)
                      and drain_visible):
                    state.delayed_candidate_ids.add(cid)
                if (
                    status == "processing" and doi and expires and expires > now_dt
                    and bool(expected_profile_hash)
                    and _relevance_claimable(item, expected_profile_hash)
                ):
                    state.processing_by_doi.setdefault(doi, cid)
                elif status == "emitted" and doi:
                    ref = EmittedPrimaryRef(cid, path, dict(item))
                    state.emitted_by_doi[doi] = select_stable_emitted_primary(
                        state.emitted_by_doi.get(doi), ref,
                    )
                if status in DURABLE_DOI_CANDIDATE_STATES and doi:
                    state.terminal_by_doi.setdefault(doi, []).append(cid)
        return index

    def claimable(self, keyword_ids: Iterable[str] | None = None) -> list[CandidateRef]:
        with self._lock:
            self._promote_due_candidates()
            self.lookups += 1
            wanted = set(keyword_ids or self._state.claimable_by_keyword)
            return [
                self._state.candidate_by_id[cid]
                for keyword in sorted(wanted)
                for cid in self._state.claimable_by_keyword.get(keyword, ())
                if _relevance_claimable(
                    self._state.candidate_by_id[cid].payload,
                    self.active_profile_hashes.get(keyword),
                )
            ]

    # ── Reader accessors (Phase 1.4) ────────────────────────────────────
    # External modules must use these instead of reading mutable fields
    # directly.  Each method either holds ``_lock`` or captures an
    # immutable snapshot.

    def get_candidate_ref(self, candidate_id: str) -> CandidateRef | None:
        """Return the :class:`CandidateRef` for *candidate_id* or ``None``."""
        with self._lock:
            return self._state.candidate_by_id.get(candidate_id)

    def get_emitted_primary(self, doi: str) -> EmittedPrimaryRef | None:
        """Return the stable emitted primary for *doi* or ``None``."""
        with self._lock:
            return self._state.emitted_by_doi.get(doi)

    def get_processing_owner(self, doi: str) -> str:
        """Return the candidate_id currently processing *doi* (``""`` if none)."""
        with self._lock:
            return self._state.processing_by_doi.get(doi, "")

    def has_page(self, page_path: Path) -> bool:
        """Return ``True`` when *page_path* is in the index cache."""
        with self._lock:
            return page_path in self._state.page_cache

    def get_page_keyword_id(self, page_path: Path) -> str:
        """Return the ``keyword_id`` for a cached page (``""`` if unknown)."""
        with self._lock:
            page = self._state.page_cache.get(page_path)
            return str(page.get("keyword_id") or "") if page is not None else ""

    def get_active_profile_hash(self, keyword_id: str) -> str | None:
        """Return the active profile hash for *keyword_id* or ``None``."""
        with self._lock:
            return self.active_profile_hashes.get(keyword_id)

    def page_count_for_keyword(self, keyword_id: str) -> int:
        """Return the number of cached pages belonging to *keyword_id*."""
        with self._lock:
            return sum(
                1 for page in self._state.page_cache.values()
                if page.get("keyword_id") == keyword_id
            )

    def get_cached_emitted_validation(
        self, key: tuple[str, int, int, str, int, int],
    ) -> tuple[bool, str] | None:
        """Return a cached emitted-validation result or ``None``."""
        with self._lock:
            return self.emitted_validation_cache.get(key)

    def set_cached_emitted_validation(
        self,
        key: tuple[str, int, int, str, int, int],
        value: tuple[bool, str],
        *,
        manifest_identity: str,
        jsonl_identity: str,
    ) -> None:
        """Store an emitted-validation result, evicting stale keys first."""
        with self._lock:
            for old_key in list(self.emitted_validation_cache):
                if old_key[0] == manifest_identity and old_key[3] == jsonl_identity:
                    self.emitted_validation_cache.pop(old_key, None)
            self.emitted_validation_cache[key] = value

    def add_page(self, path: Path, page: Mapping[str, Any]) -> None:
        """Publish a freshly persisted page as a copy-on-write replacement.

        All old projections for the same page are removed before the new ones
        are inserted.  Cross-page ``candidate_id`` collisions fail closed.
        On any error the index is left unchanged — the atomic ``_state``
        swap only happens after all validation passes.
        """
        materialized = dict(page)
        materialized["checksum"] = _compute_checksum(materialized)
        validate_page(materialized, path)
        with self._lock:
            keyword_id = str(page["keyword_id"])
            expected_profile_hash = self.active_profile_hashes.get(keyword_id)
            now_dt = datetime.now(timezone.utc)

            # ── Single copy-on-write clone of the entire state ──────────
            new_state = self._state.copy()

            # ── Remove old projections for this page ────────────────────
            old_cids = {
                cid for cid, ref in new_state.candidate_by_id.items()
                if ref.page_path == path
            }
            for cid in old_cids:
                del new_state.candidate_by_id[cid]
            for kw_queue in new_state.claimable_by_keyword.values():
                survivors = [cid for cid in kw_queue if cid not in old_cids]
                kw_queue.clear()
                kw_queue.extend(survivors)
            new_state.delayed_candidate_ids.difference_update(old_cids)
            for doi, cid in list(new_state.processing_by_doi.items()):
                if cid in old_cids:
                    del new_state.processing_by_doi[doi]
            for doi, ref in list(new_state.emitted_by_doi.items()):
                if ref.candidate_id in old_cids:
                    del new_state.emitted_by_doi[doi]
            for doi in list(new_state.terminal_by_doi):
                new_state.terminal_by_doi[doi] = [
                    cid for cid in new_state.terminal_by_doi[doi]
                    if cid not in old_cids
                ]
                if not new_state.terminal_by_doi[doi]:
                    del new_state.terminal_by_doi[doi]

            # ── Insert new page projections ─────────────────────────────
            new_state.page_cache[path] = materialized
            new_queue = new_state.claimable_by_keyword.setdefault(keyword_id, deque())
            drain_visible = page_is_drain_visible(page)
            for raw in page.get("candidates", []):
                item = dict(raw)
                cid = str(item.get("candidate_id") or "")
                if not cid:
                    continue
                existing = new_state.candidate_by_id.get(cid)
                if existing is not None and existing.page_path != path:
                    raise JournalCorruptError(
                        f"candidate_id collision: {cid} already on "
                        f"{existing.page_path}, cannot add from {path}"
                    )
                new_state.candidate_by_id[cid] = CandidateRef(
                    cid, keyword_id, path, item)
                status = str(item.get("status") or "")
                expires = parse_iso(item.get("lease_expires_at"))
                next_attempt = parse_iso(item.get("next_attempt_at"))
                if (
                    drain_visible
                    and (not next_attempt or next_attempt <= now_dt)
                    and (
                        status in {"pending", "ready", "failed_retryable"}
                        or status == "processing"
                        and (not expires or expires <= now_dt)
                    )
                    and bool(expected_profile_hash)
                    and _relevance_claimable(item, expected_profile_hash)
                ):
                    new_queue.append(cid)
                elif (
                    drain_visible
                    and next_attempt and next_attempt > now_dt
                    and status in {"pending", "ready", "failed_retryable"}
                    and bool(expected_profile_hash)
                    and _relevance_claimable(item, expected_profile_hash)
                ):
                    new_state.delayed_candidate_ids.add(cid)
                doi = _candidate_doi(item)
                if status == "emitted" and doi:
                    ref = EmittedPrimaryRef(cid, path, dict(item))
                    new_state.emitted_by_doi[doi] = select_stable_emitted_primary(
                        new_state.emitted_by_doi.get(doi), ref,
                    )
                if status in DURABLE_DOI_CANDIDATE_STATES and doi:
                    new_state.terminal_by_doi.setdefault(doi, []).append(cid)

            # ── Single atomic swap — readers see old or new, never mixed ─
            self._state = new_state
            self.dirty_pages.add(path)

    def pending_count(self, keyword_ids: Iterable[str] | None = None) -> int:
        with self._lock:
            self._promote_due_candidates()
            self.lookups += 1
            wanted = set(keyword_ids or self._state.claimable_by_keyword)
            return sum(
                1
                for keyword in wanted
                for cid in self._state.claimable_by_keyword.get(keyword, ())
                if _relevance_claimable(
                    self._state.candidate_by_id[cid].payload,
                    self.active_profile_hashes.get(keyword),
                )
            )

    def update_candidate(self, page_path: Path, item: Mapping[str, Any]) -> None:
        with self._lock:
            cid = str(item.get("candidate_id") or "")
            old = self._state.candidate_by_id.get(cid)
            keyword_id = old.keyword_id if old else str(
                (self._state.page_cache.get(page_path) or {}).get("keyword_id") or "")
            materialized = dict(item)

            # Copy-on-write clone.
            new_state = self._state.copy()
            new_state.candidate_by_id[cid] = CandidateRef(
                cid, keyword_id, page_path, materialized)
            self.dirty_pages.add(page_path)
            new_state.delayed_candidate_ids.discard(cid)
            for queue in new_state.claimable_by_keyword.values():
                try:
                    queue.remove(cid)
                except ValueError:
                    pass
            for doi, owner in list(new_state.processing_by_doi.items()):
                if owner == cid:
                    new_state.processing_by_doi.pop(doi, None)
            for doi, ref in list(new_state.emitted_by_doi.items()):
                if ref.candidate_id == cid:
                    new_state.emitted_by_doi.pop(doi, None)
            for doi, owners in list(new_state.terminal_by_doi.items()):
                if cid in owners:
                    new_state.terminal_by_doi[doi] = [
                        owner for owner in owners if owner != cid]
                    if not new_state.terminal_by_doi[doi]:
                        new_state.terminal_by_doi.pop(doi, None)
            doi = _candidate_doi(materialized)
            status = str(item.get("status") or "")
            expires = parse_iso(item.get("lease_expires_at"))
            next_attempt = parse_iso(item.get("next_attempt_at"))
            now_dt = datetime.now(timezone.utc)
            page = new_state.page_cache.get(page_path)
            expected_profile_hash = self.active_profile_hashes.get(keyword_id)
            if (
                status == "processing" and doi and expires and expires > now_dt
                and bool(expected_profile_hash)
                and _relevance_claimable(materialized, expected_profile_hash)
            ):
                new_state.processing_by_doi.setdefault(doi, cid)
            elif status == "emitted" and doi:
                ref = EmittedPrimaryRef(cid, page_path, materialized)
                new_state.emitted_by_doi[doi] = select_stable_emitted_primary(
                    new_state.emitted_by_doi.get(doi), ref,
                )
            if status in DURABLE_DOI_CANDIDATE_STATES and doi:
                new_state.terminal_by_doi.setdefault(doi, []).append(cid)
            if (
                page is not None and page_is_drain_visible(page)
                and (
                    status in {"pending", "ready", "failed_retryable"}
                    or status == "processing" and (not expires or expires <= now_dt)
                )
                and bool(expected_profile_hash)
                and _relevance_claimable(materialized, expected_profile_hash)
            ):
                if not next_attempt or next_attempt <= now_dt:
                    new_state.claimable_by_keyword.setdefault(
                        keyword_id, deque()).append(cid)
                else:
                    new_state.delayed_candidate_ids.add(cid)

            # Single atomic swap.
            self._state = new_state

    def _promote_due_candidates(self) -> None:
        now_dt = datetime.now(timezone.utc)
        for cid in tuple(self._state.delayed_candidate_ids):
            ref = self._state.candidate_by_id.get(cid)
            if ref is None:
                self._state.delayed_candidate_ids.discard(cid)
                continue
            next_attempt = parse_iso(ref.payload.get("next_attempt_at"))
            page = self._state.page_cache.get(ref.page_path)
            if (not next_attempt or next_attempt <= now_dt) and (
                page is not None and page_is_drain_visible(page)
            ):
                queue = self._state.claimable_by_keyword.setdefault(
                    ref.keyword_id, deque())
                expected_profile_hash = self.active_profile_hashes.get(ref.keyword_id)
                if (
                    expected_profile_hash
                    and _relevance_claimable(ref.payload, expected_profile_hash)
                    and cid not in queue
                ):
                    queue.append(cid)
                self._state.delayed_candidate_ids.discard(cid)

    def apply_relevance_updates(
        self, page_path: Path, candidates: Iterable[Mapping[str, Any]],
    ) -> None:
        """Incrementally publish persisted deferred-relevance decisions."""
        for candidate in candidates:
            self.update_candidate(page_path, candidate)
            self.relevance_updates += 1

    def assert_active_bindings(self, expected: Mapping[str, str]) -> None:
        materialized = {str(key): str(value) for key, value in expected.items()}
        if self.active_profile_hashes != materialized:
            self.binding_invariant_failures += 1
            raise RuntimeError("journal index active relevance bindings drifted")

from src.discovery.contracts.page_journal import (
    CandidateRef,
    DURABLE_DOI_CANDIDATE_STATES,
    EmittedPrimaryRef,
    JournalCorruptError,
    _candidate_doi,
    _compute_checksum,
    _relevance_claimable,
    page_is_drain_visible,
    parse_iso,
    select_stable_emitted_primary,
    validate_page,
)

from src.discovery.stores.page_journal_store import PageJournalStoreV4
