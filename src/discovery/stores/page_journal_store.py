"""PageJournalStoreV4 — canonical self-contained v4 page journal store.

This module owns the durable read/write surface for schema 4.0 provider
page journals.  It does not inherit from the legacy top-level class.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from filelock import FileLock

from src.discovery.contracts.page_journal import (
    INITIAL_CURSOR,
    PAGE_SCHEMA_VERSION,
    PAGE_TRANSITIONS,
    RELEVANCE_STATES,
    TERMINAL_CANDIDATE_STATES,
    CandidateClaim,
    CandidateState,
    ClaimResult,
    InvalidStateTransition,
    JournalCorruptError,
    PageLane,
    PageRef,
    PageState,
    all_terminal,
    candidate_doi,
    compute_checksum,
    compute_statistics,
    make_candidate_record,
    now_iso,
    page_is_drain_visible,
    parse_iso,
    relevance_claimable,
    relevance_state,
    request_signature,
    stable_hash,
    transform_page_for_profile_closure,
    validate_page,
)
from src.discovery.stores.page_journal_ops import (
    assert_relevance_finalized,
    assert_terminal_replay_equivalent,
    path_is_reparse,
    transition_candidate,
    write_page_json_unlocked,
)
from src.discovery.execution.lane_models import (
    DiscoveryLaneKey,
    RequestSignature,
)
from src.discovery.contracts.lane_history import (
    ExhaustionEvidence,
    ProviderResponseMetadata,
)
from src.discovery.models import PaperCandidate
from src.discovery.relevance import RelevanceReason
from src.discovery.workspace import DiscoveryWorkspace
from src.utils.atomic_io import atomic_replace_bytes_unlocked

class PageJournalStoreV4:
    """File-backed page journal store.

    Lock order rule for candidate drain: acquire page locks only for short
    claim/commit mutations. Never wait for DOI, resolution, export, or
    ``paper_raw`` locks while holding a page lock.
    """

    def __init__(self, root_dir: DiscoveryWorkspace | str | Path) -> None:
        # Duck-typing is used deliberately so that test reloads of
        # src.discovery.workspace do not break store identity checks.
        if hasattr(root_dir, "page_journals_dir"):
            self._workspace = root_dir  # type: ignore[assignment]
            self.root_dir = Path(root_dir.page_journals_dir)  # type: ignore[arg-type]
        else:
            self._workspace = None
            self.root_dir = Path(root_dir)

    @property
    def workspace(self) -> DiscoveryWorkspace | None:
        return self._workspace

    def list_all(self) -> list[Path]:
        """List all page journal files recursively."""
        if not self.root_dir.is_dir():
            return []
        return sorted(self.root_dir.rglob("*.json"))

    def list_by_keyword(self, keyword_id: str) -> list[Path]:
        """List page journal files for one keyword."""
        kd = self.root_dir / keyword_id
        if not kd.is_dir():
            return []
        return sorted(kd.rglob("*.json"))

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

    def _validate_page_path_identity(self, data: dict[str, Any], path: Path) -> None:
        """Reject pages whose filesystem path disagrees with their content identity.

        Uses the same canonical path builder as ``write_page()`` so the
        directory layout is defined in exactly one place.
        """
        resolved_root = self.root_dir.resolve()
        resolved_path = path.resolve()
        # 1. Resolved page must be inside resolved root.
        try:
            resolved_path.relative_to(resolved_root)
        except ValueError:
            raise JournalCorruptError(f"page outside journal root: {path}")
        # 2. No symlink or reparse point in components between root and leaf.
        #    The root itself may be a symlink/junction.
        current = path
        while True:
            try:
                current.relative_to(self.root_dir)
            except ValueError:
                break  # walked past root boundary
            if current == self.root_dir or current == self.root_dir.resolve():
                break
            if path_is_reparse(current):
                raise JournalCorruptError(f"symlink/reparse in journal path: {current}")
            current = current.parent
        # 3. The path must match the canonical identity derived from content.
        expected = self.page_path(
            keyword_id=data["keyword_id"],
            query_id=data["query_id"],
            provider=data["provider"],
            lane=data["lane"],
            page_id=data["page_id"],
        )
        if resolved_path != expected.resolve():
            raise JournalCorruptError(
                f"journal path identity mismatch: {path} vs expected {expected}"
            )

    def read(self, path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise JournalCorruptError(f"journal JSON corrupt: {path}: {exc}") from exc
        result = validate_page(data, path)
        self._validate_page_path_identity(result, path)
        return result

    def write_page(self, page: dict[str, Any]) -> Path:
        page["checksum"] = compute_checksum(page)
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
            write_page_json_unlocked(path, validate_page(page, path))
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
        lane_key: DiscoveryLaneKey,
        request_signature_value: dict[str, Any],
        request_cursor: str | None,
        next_cursor: str | None,
        provider_exhausted: bool,
        response_metadata: ProviderResponseMetadata,
        exhaustion_evidence: ExhaustionEvidence | None,
        candidates: list[PaperCandidate],
        generation: int = 1,
        refresh_run_id: str | None = None,
        page_sequence: int | None = None,
        state: PageState = "fetched",
        relevance_profile_hash: str | None = None,
    ) -> dict[str, Any]:
        now = now_iso()
        records = [
            make_candidate_record(
                page_id, cand, idx, relevance_profile_hash=relevance_profile_hash
            )
            for idx, cand in enumerate(candidates)
        ]
        if request_cursor is None:
            raise ValueError("durable provider pages require a concrete request_cursor")
        if lane_key.to_dict() != {
            "keyword_id": keyword_id,
            "query_id": query_id,
            "provider": provider,
            "mode": lane,
            "generation": int(generation),
            "request_signature": str(request_signature_value.get("hash") or ""),
        }:
            raise ValueError("lane_key does not match durable page identity")
        if provider_exhausted and exhaustion_evidence is None:
            raise ValueError("exhausted durable provider page requires exhaustion_evidence")
        if not provider_exhausted and exhaustion_evidence is not None:
            raise ValueError("non-exhausted durable provider page must not carry exhaustion_evidence")
        page_dict: dict[str, Any] = {
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
            "lane_key": lane_key.to_dict(),
            "refresh_run_id": refresh_run_id,
            "page_sequence": page_sequence,
            "request_signature": request_signature_value,
            "request_cursor": request_cursor,
            "next_cursor": next_cursor,
            "provider_exhausted": bool(provider_exhausted),
            "returned_count": len(records),
            "response_metadata": response_metadata.to_dict(),
            "exhaustion_evidence": (
                None if exhaustion_evidence is None else exhaustion_evidence.to_dict()
            ),
            "state": state,
            "fetched_at": now,
            "cursor_committed_at": now if state == "cursor_committed" else None,
            "drained_at": None,
            "candidates": records,
            "statistics": compute_statistics(records),
            "checksum": "",  # placeholder for computation
        }
        page_dict["checksum"] = compute_checksum(page_dict)
        return page_dict

    def make_synthetic_page(self, **kwargs: Any) -> dict[str, Any]:
        """Build a complete v4 page for an isolated test fixture.

        Production code must call :meth:`make_page` with the real response
        metadata supplied by ``ProviderPageFetcher``.  Tests sometimes need a
        durable page without making a provider request; this explicit helper
        creates *synthetic but complete* evidence rather than reviving the
        removed v2/hash-only journal shape.  No runtime path calls it.
        """
        values = dict(kwargs)
        page_id = str(values["page_id"])
        keyword_id_value = str(values["keyword_id"])
        query_id_value = str(values["query_id"])
        provider = str(values["provider"])
        lane = str(values["lane"])
        generation = int(values.get("generation", 1))
        signature_value = values.get("request_signature_value")
        if signature_value is None:
            signature_value = request_signature(page_size=50)
        if not isinstance(signature_value, dict):
            raise TypeError("synthetic request_signature_value must be object")
        signature = RequestSignature.from_dict_strict(signature_value)
        values["request_signature_value"] = signature.to_dict()
        values.setdefault("generation", generation)
        # Older test fixtures commonly expressed the first request as None.
        # The synthetic helper turns that into the explicit durable sentinel;
        # production pages remain strict and never receive this normalization.
        if values.get("request_cursor") is None:
            values["request_cursor"] = INITIAL_CURSOR
        values.setdefault(
            "lane_key",
            DiscoveryLaneKey(
                keyword_id=keyword_id_value,
                query_id=query_id_value,
                provider=provider,  # type: ignore[arg-type]
                mode=lane,  # type: ignore[arg-type]
                generation=generation,
                request_signature=signature.hash,
            ),
        )
        candidates = values.get("candidates") or []
        metadata_value = values.get("response_metadata")
        if metadata_value is None:
            metadata = ProviderResponseMetadata(
                http_status=200,
                total_results=len(candidates),
                next_cursor_present=values.get("next_cursor") is not None,
                response_fingerprint=stable_hash(
                    "synthetic-provider-page",
                    page_id,
                    signature.hash,
                    values.get("request_cursor"),
                    values.get("next_cursor"),
                    len(candidates),
                    length=64,
                ),
                observed_at=now_iso(),
            )
        elif isinstance(metadata_value, ProviderResponseMetadata):
            metadata = metadata_value
        elif isinstance(metadata_value, Mapping):
            metadata = ProviderResponseMetadata.from_dict_strict(metadata_value)
        else:
            raise TypeError("synthetic response_metadata must be metadata object")
        values["response_metadata"] = metadata
        exhausted = bool(values.get("provider_exhausted", False))
        evidence_value = values.get("exhaustion_evidence")
        if exhausted and evidence_value is None:
            values["exhaustion_evidence"] = ExhaustionEvidence(
                provider=provider,
                query_id=query_id_value,
                request_signature=signature.hash,
                generation=generation,
                cursor_before=str(values["request_cursor"]),
                response_metadata=metadata,
                observed_at=metadata.observed_at,
            )
        elif isinstance(evidence_value, Mapping):
            values["exhaustion_evidence"] = ExhaustionEvidence.from_dict_strict(evidence_value)
        elif evidence_value is None:
            values["exhaustion_evidence"] = None
        return self.make_page(**values)

    def transition_page(self, path: Path, new_state: PageState) -> dict[str, Any]:
        with self.lock_for(path):
            data = self.read(path)
            old = data["state"]
            if new_state not in PAGE_TRANSITIONS[old]:
                raise InvalidStateTransition(f"page {old} -> {new_state} is not allowed")
            data["state"] = new_state
            if new_state == "cursor_committed":
                data["cursor_committed_at"] = now_iso()
            if new_state == "drained":
                data["drained_at"] = now_iso()
            data["statistics"] = compute_statistics(data["candidates"])
            data["checksum"] = compute_checksum(data)
            write_page_json_unlocked(path, data)
            return data

    def mark_cursor_committed(self, path: Path) -> dict[str, Any]:
        with self.lock_for(path):
            data = self.read(path)
            if data["state"] == "cursor_committed":
                return data
            if data["state"] != "fetched":
                raise InvalidStateTransition(f"cannot mark cursor_committed from {data['state']}")
            # ── Universal relevance barrier: every candidate must carry an ──
            #     explicit, non-profile_unbound relevance record BEFORE the
            #     cursor advances.  This covers the all-terminal fast-path
            #     below as well as the normal cursor-commit path.
            assert_relevance_finalized(data, path)
            data["state"] = "cursor_committed"
            data["cursor_committed_at"] = now_iso()
            # A profile-bound page with only rejected/invalid relevance
            # decisions has no candidate work left to drain.  Preserve the
            # legacy cursor-commit state for pre-profile pages, but close
            # explicit relevance pages immediately.
            if (
                data["candidates"]
                and all(isinstance(item.get("relevance"), Mapping) for item in data["candidates"])
                and all_terminal(data["candidates"])
            ):
                data["state"] = "drained"
                data["drained_at"] = data.get("drained_at") or now_iso()
            data["statistics"] = compute_statistics(data["candidates"])
            data["checksum"] = compute_checksum(data)
            write_page_json_unlocked(path, data)
            return data

    def finalize_relevance(
        self,
        path: Path,
        decisions: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Persist notebook-local relevance decisions before cursor CAS.

        This mutation is intentionally valid only while a page is ``fetched``.
        A second invocation is idempotent for already materialized decisions,
        but it refuses to rewrite a page whose cursor has already advanced.
        Candidate lifecycle fields are never changed here.
        """
        with self.lock_for(path):
            data = self.read(path)
            if data["state"] != "fetched":
                if data["state"] in {"cursor_committed", "draining", "drained"}:
                    return data
                raise InvalidStateTransition(
                    f"cannot finalize relevance for page state {data['state']}"
                )
            by_id = {str(key): value for key, value in decisions.items()}
            seen: set[str] = set()
            for item in data["candidates"]:
                cid = str(item.get("candidate_id") or "")
                decision = by_id.get(cid)
                if decision is None:
                    continue
                if not isinstance(decision, Mapping):
                    raise JournalCorruptError(f"relevance decision must be object: {path}")
                new_state = str(decision.get("state") or "")
                if new_state not in RELEVANCE_STATES or new_state == "profile_unbound":
                    raise InvalidStateTransition(f"invalid relevance decision {new_state!r}")
                old_state = (
                    relevance_state(item)
                    if isinstance(item.get("relevance"), Mapping)
                    else "profile_unbound"
                )
                allowed = {
                    "profile_unbound": RELEVANCE_STATES - {"profile_unbound"},
                    "verification_deferred": RELEVANCE_STATES - {"profile_unbound"},
                    "passed": {"passed"},
                    "rejected": {"rejected"},
                    "candidate_invalid": {"candidate_invalid"},
                }.get(old_state, set())
                if new_state not in allowed:
                    raise InvalidStateTransition(
                        f"relevance {old_state} -> {new_state} is not allowed"
                    )
                materialized = dict(decision)
                materialized.setdefault("profile_hash", "")
                materialized.setdefault("matched_groups", {})
                materialized.setdefault("negative_matches", [])
                materialized.setdefault("reason", "")
                materialized.setdefault("verification", {})
                materialized.setdefault("attempt_count", 0)
                materialized.setdefault("next_retry_at", None)
                materialized.setdefault("last_attempt_at", None)
                materialized.setdefault("last_error_class", None)
                materialized.setdefault("last_http_status", None)
                old_relevance = item.get("relevance")
                old_profile_hash = (
                    str(old_relevance.get("profile_hash") or "")
                    if isinstance(old_relevance, Mapping) else ""
                )
                decision_profile_hash = str(materialized.get("profile_hash") or "")
                if old_profile_hash and decision_profile_hash != old_profile_hash:
                    raise InvalidStateTransition(
                        f"relevance profile hash changed for candidate {cid}"
                    )
                item["relevance"] = materialized
                seen.add(cid)
            unknown = sorted(set(by_id) - seen)
            if unknown:
                raise KeyError(f"relevance decisions reference unknown candidates: {unknown}")
            still_unbound = sorted(
                str(item.get("candidate_id") or "")
                for item in data["candidates"]
                if relevance_state(item) == "profile_unbound"
            )
            if still_unbound:
                raise InvalidStateTransition(
                    "relevance finalization left profile_unbound candidates: "
                    + ",".join(still_unbound)
                )
            data["statistics"] = compute_statistics(data["candidates"])
            data["checksum"] = compute_checksum(data)
            write_page_json_unlocked(path, data)
            return data

    def close_stale_profile_candidates(
        self,
        path: Path,
        *,
        new_profile_hash: str,
        planned_mutations: tuple[Mapping[str, Any], ...],
        closure_timestamp: str,
        transaction_id: str,
        reason: RelevanceReason = RelevanceReason.STALE_PROFILE_CLOSED_BY_PROFILE_APPLY,
    ) -> dict[str, Any]:
        """Reject every nonterminal relevance verdict from an older profile.

        Page ``generation`` belongs to the provider lane and is deliberately
        not consulted here.  Candidate receipts and the request signature are
        the profile identity facts.
        """
        with self.lock_for(path):
            transformed = transform_page_for_profile_closure(
                path.read_bytes(),
                planned_mutations=planned_mutations,
                closure_timestamp=closure_timestamp,
                transaction_id=transaction_id,
                reason=reason,
                target_profile_hash=new_profile_hash,
            )
            data = json.loads(transformed.decode("utf-8"))
            atomic_replace_bytes_unlocked(path, transformed)
            return data

    def retry_deferred_relevance(
        self,
        path: Path,
        decisions: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Update due deferred decisions without reopening a committed page."""
        with self.lock_for(path):
            data = self.read(path)
            by_id = {str(key): value for key, value in decisions.items()}
            for item in data["candidates"]:
                cid = str(item.get("candidate_id") or "")
                if cid not in by_id:
                    continue
                if relevance_state(item) != "verification_deferred":
                    continue
                decision = by_id[cid]
                state = str(decision.get("state") or "")
                if state not in RELEVANCE_STATES - {"profile_unbound"}:
                    raise InvalidStateTransition(f"invalid deferred relevance state {state!r}")
                old_hash = str((item.get("relevance") or {}).get("profile_hash") or "")
                new_hash = str(decision.get("profile_hash") or "")
                if old_hash and old_hash != new_hash:
                    raise InvalidStateTransition(
                        f"deferred relevance profile hash changed for candidate {cid}"
                    )
                item["relevance"] = dict(decision)
            data["statistics"] = compute_statistics(data["candidates"])
            if all_terminal(data["candidates"]) and data["state"] in {"cursor_committed", "draining"}:
                data["state"] = "drained"
                data["drained_at"] = data.get("drained_at") or now_iso()
            data["checksum"] = compute_checksum(data)
            write_page_json_unlocked(path, data)
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

    def claim_candidate(
        self,
        page_path: Path,
        *,
        candidate_id_value: str,
        worker_id: str,
        lease_seconds: int,
        expected_profile_hash: str,
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
                if not relevance_claimable(item, expected_profile_hash):
                    return ClaimResult(False, page_path, candidate_id_value, reason="relevance_not_passed")
                if status not in {"pending", "ready", "failed_retryable", "processing"}:
                    return ClaimResult(False, page_path, candidate_id_value, reason=f"not_claimable:{status}")
                transition_candidate(item, "processing")
                item["claimed_by"] = worker_id
                item["claimed_at"] = now_iso()
                item["lease_expires_at"] = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
                item["attempts"] = int(item.get("attempts") or 0) + 1
                data["statistics"] = compute_statistics(data["candidates"])
                data["checksum"] = compute_checksum(data)
                write_page_json_unlocked(page_path, data)
                return ClaimResult(True, page_path, candidate_id_value, candidate=dict(item))
        return ClaimResult(False, page_path, candidate_id_value, reason="candidate_not_found")

    def claim_candidates_from_page(self, page_path: Path, *, worker_id: str,
                                   lease_seconds: int, limit: int = 16,
                                   candidate_ids: Iterable[str] | None = None,
                                   expected_profile_hash: str | None = None) -> list[CandidateClaim]:
        """Claim up to ``limit`` candidates with one page read and one fsync."""
        if limit < 1:
            return []
        wanted = set(candidate_ids or [])
        claims: list[CandidateClaim] = []
        with self.lock_for(page_path):
            data = self.read(page_path)
            if data["state"] not in {"cursor_committed", "draining"}:
                return claims
            now_dt = datetime.now(timezone.utc)
            if data["state"] == "cursor_committed":
                data["state"] = "draining"
            for item in data["candidates"]:
                if len(claims) >= limit:
                    break
                cid = str(item.get("candidate_id") or "")
                if wanted and cid not in wanted:
                    continue
                expires = parse_iso(item.get("lease_expires_at"))
                next_attempt = parse_iso(item.get("next_attempt_at"))
                status = item.get("status")
                if not relevance_claimable(item, expected_profile_hash):
                    continue
                if next_attempt and next_attempt > now_dt:
                    continue
                if status == "processing" and expires and expires > now_dt:
                    continue
                if status not in {"pending", "ready", "failed_retryable", "processing"}:
                    continue
                transition_candidate(item, "processing")
                item["claimed_by"] = worker_id
                item["claimed_at"] = now_iso()
                item["lease_expires_at"] = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
                item["attempts"] = int(item.get("attempts") or 0) + 1
                claims.append(CandidateClaim(
                    cid, str(data["keyword_id"]), str(data["page_id"]),
                    str(data["provider"]), candidate_doi(item), page_path,
                    dict(item), str(item["lease_expires_at"])))
            if claims:
                data["statistics"] = compute_statistics(data["candidates"])
                data["checksum"] = compute_checksum(data)
                write_page_json_unlocked(page_path, data)
        return claims

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
                    data["checksum"] = compute_checksum(data)
                    write_page_json_unlocked(page_path, data)
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
                transition_candidate(item, "failed_retryable")
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
                data["statistics"] = compute_statistics(data["candidates"])
                data["checksum"] = compute_checksum(data)
                write_page_json_unlocked(page_path, data)
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
                if assert_terminal_replay_equivalent(
                    item, new_status=new_status, updates=updates,
                ):
                    return dict(item)
                if item.get("status") == "processing" and item.get("claimed_by") != worker_id:
                    raise InvalidStateTransition("only claim owner may commit processing result")
                transition_candidate(item, new_status)
                if updates:
                    item.update(updates)
                if new_status in TERMINAL_CANDIDATE_STATES or new_status == "failed_retryable":
                    item["claimed_by"] = None
                    item["claimed_at"] = None
                    item["lease_expires_at"] = None
                data["statistics"] = compute_statistics(data["candidates"])
                if all_terminal(data["candidates"]):
                    if data["state"] != "drained":
                        if data["state"] not in {"cursor_committed", "draining"}:
                            raise InvalidStateTransition(f"cannot drain page from {data['state']}")
                        data["state"] = "drained"
                        data["drained_at"] = now_iso()
                data["checksum"] = compute_checksum(data)
                write_page_json_unlocked(page_path, data)
                return dict(item)
        raise KeyError(f"candidate not found: {candidate_id_value}")

    def commit_candidate_results(self, page_path: Path, results: Iterable[Mapping[str, Any]],
                                 *, worker_id: str) -> list[dict[str, Any]]:
        """Merge multiple candidate outcomes into one atomic page write."""
        by_id = {str(result["candidate_id"]): result for result in results}
        if not by_id:
            return []
        committed: list[dict[str, Any]] = []
        changed = False
        with self.lock_for(page_path):
            data = self.read(page_path)
            for item in data["candidates"]:
                result = by_id.get(str(item.get("candidate_id") or ""))
                if result is None:
                    continue
                new_status = str(result["new_status"])
                updates = result.get("updates")
                if assert_terminal_replay_equivalent(
                    item,
                    new_status=new_status,
                    updates=updates if isinstance(updates, Mapping) else None,
                ):
                    committed.append(dict(item))
                    continue
                if item.get("status") == "processing" and item.get("claimed_by") != worker_id:
                    raise InvalidStateTransition("only claim owner may commit processing result")
                transition_candidate(item, new_status)  # type: ignore[arg-type]
                if isinstance(updates, Mapping):
                    item.update(updates)
                if new_status in TERMINAL_CANDIDATE_STATES or new_status == "failed_retryable":
                    item["claimed_by"] = None
                    item["claimed_at"] = None
                    item["lease_expires_at"] = None
                committed.append(dict(item))
                changed = True
            if len(committed) != len(by_id):
                missing = sorted(set(by_id) - {str(item["candidate_id"]) for item in committed})
                raise KeyError(f"candidates not found: {','.join(missing)}")
            if changed:
                data["statistics"] = compute_statistics(data["candidates"])
                if all_terminal(data["candidates"]):
                    if data["state"] not in {"cursor_committed", "draining", "drained"}:
                        raise InvalidStateTransition(f"cannot drain page from {data['state']}")
                    data["state"] = "drained"
                    data["drained_at"] = data.get("drained_at") or now_iso()
                data["checksum"] = compute_checksum(data)
                write_page_json_unlocked(page_path, data)
        return committed

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
                data["checksum"] = compute_checksum(data)
                validate_page(data, page_path)
                write_page_json_unlocked(page_path, data)
                return dict(item)
        raise KeyError(f"candidate not found: {candidate_id_value}")

