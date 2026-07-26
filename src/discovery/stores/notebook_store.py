"""NotebookStoreV4 — canonical self-contained v4 keyword notebook store.

This module owns the durable read/write surface for schema 4.0 keyword
notebooks.  It does not delegate to any legacy top-level class.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from filelock import FileLock

from src.discovery.constants import (
    BACKFILL_STATE_ACCEPTED_FIELDS,
    BACKFILL_STATE_FIELDS,
    INITIAL_CURSOR,
)
from src.discovery.contracts.notebook import (
    CursorConflictError,
    DiscoveryNotReadyError,
    NotebookCorruptError,
    UnsupportedNotebookSchemaError,
    PROVIDERS,
    _HAS_CJK,
    _HEX16,
    _active_queries,
    _empty_backfill_state,
    _empty_search_query,
    _now_iso,
    detect_query_language,
    empty_notebook,
    keyword_fingerprint8,
    keyword_id,
    normalize_keyword,
    notebook_path,
    query_identity,
    resolve_existing_notebook,
    safe_slug,
    validate_discovery_readiness,
    validate_notebook,
)
from src.discovery.workspace import DiscoveryWorkspace
from src.utils.atomic_io import _fsync_dir as _fsync_dir_if_posix

@dataclass
class LaneRunResult:
    """Summary of one lane run for the per-keyword report."""
    lane: str
    status: str
    pages: int
    items_returned: int
    provider_failures: int
    exhausted_states: int


class NotebookStoreV4:
    """File-backed store with per-keyword locking and field-level merge.

    Accepts ONLY v4 notebooks.  v1/v2/v4 notebooks raise
    ``UnsupportedNotebookSchemaError`` on load and must be migrated.
    """

    def __init__(self, workspace: DiscoveryWorkspace | Path | str) -> None:
        # Duck-typing is used deliberately so that test reloads of
        # src.discovery.workspace do not break store identity checks.
        if hasattr(workspace, "keyword_notebook_dir"):
            self._workspace = workspace  # type: ignore[assignment]
            self._dir = Path(workspace.keyword_notebook_dir)  # type: ignore[arg-type]
        else:
            self._workspace = None
            self._dir = Path(workspace)  # type: ignore[arg-type]
        self.notebook_dir = self._dir

    @property
    def workspace(self) -> DiscoveryWorkspace | None:
        return self._workspace

    # ── path / lock resolution ───────────────────────────────────────

    def _path_for(self, keyword: str) -> Path:
        existing = resolve_existing_notebook(keyword, self.notebook_dir)
        return existing if existing is not None else notebook_path(keyword, self.notebook_dir)

    def _lock_for(self, keyword: str) -> FileLock:
        nb_path = self._path_for(keyword)
        return FileLock(str(nb_path.with_suffix(nb_path.suffix + ".lock")))

    # ── load / save ──────────────────────────────────────────────────

    def load(self, keyword: str) -> dict[str, Any] | None:
        """Load a v4 notebook or return None if absent.

        Rejects v1/v2/v4 notebooks with ``UnsupportedNotebookSchemaError``.
        Corrupt JSON raises ``NotebookCorruptError``.
        """
        path = self._path_for(keyword)
        if not path.is_file():
            return None
        with self._lock_for(keyword):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise NotebookCorruptError(f"notebook JSON corrupt: {path}: {exc}") from exc
            return validate_notebook(data)

    def require_v4(self, keyword: str) -> dict[str, Any]:
        """Load a v4 notebook or raise FileNotFoundError."""
        nb = self.load(keyword)
        if nb is None:
            raise FileNotFoundError(f"no notebook for keyword: {keyword!r}")
        return nb

    def require(self, keyword: str) -> dict[str, Any]:
        """Load a notebook or raise FileNotFoundError."""
        nb = self.load(keyword)
        if nb is None:
            raise FileNotFoundError(f"no notebook for keyword: {keyword!r}")
        return nb

    def _read_or_init(self, path: Path, keyword: str) -> dict[str, Any]:
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise NotebookCorruptError(
                    f"notebook JSON corrupt: {path}: {exc}"
                ) from exc
            return validate_notebook(data)
        return empty_notebook(keyword)

    def _save(self, path: Path, nb: dict[str, Any]) -> None:
        """Write inline (tmp + os.replace + fsync) — caller already holds the lock."""
        nb["updated_at"] = _now_iso()
        validate_notebook(nb)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write(json.dumps(nb, ensure_ascii=False, indent=2))
                fh.flush()
                os.fsync(fh.fileno())
            last_exc: Exception | None = None
            for _ in range(5):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError as exc:
                    last_exc = exc
                    time.sleep(0.05)
            else:
                if last_exc:
                    try:
                        os.replace(tmp, path)
                    except Exception:
                        raise last_exc
            _fsync_dir_if_posix(path.parent)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def _mutate(self, keyword: str, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Read-modify-write inside the per-keyword lock."""
        path = self._path_for(keyword)
        if not path.is_file():
            raise FileNotFoundError(f"no notebook for keyword: {keyword!r}")
        with FileLock(str(path.with_suffix(path.suffix + ".lock"))):
            if not path.is_file():
                raise FileNotFoundError(f"no notebook for keyword: {keyword!r}")
            nb = self._read_or_init(path, keyword)
            mutator(nb)
            self._save(path, nb)
            return nb

    # ── notebook lifecycle ────────────────────────────────────────────

    def ensure_notebook(self, keyword_zh: str) -> dict[str, Any]:
        """Get or create a v4 notebook for ``keyword_zh``.

        Does NOT touch search queries, cursors, or backfill state.
        """
        if not isinstance(keyword_zh, str) or not _HAS_CJK.search(keyword_zh):
            raise ValueError("keyword_zh must contain Chinese text")
        path = self._path_for(keyword_zh)
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(path.with_suffix(path.suffix + ".lock"))):
            if path.is_file():
                nb = self._read_or_init(path, keyword_zh)
            else:
                nb = empty_notebook(keyword_zh)
                # An empty notebook cannot satisfy bilingual discovery
                # readiness.  Create it as an explicit draft so callers may
                # build its query set before the atomic enable transition.
                nb["enabled"] = False
                self._save(path, nb)
            return nb

    def create_notebook(
        self,
        keyword_zh: str,
        *,
        classification: dict[str, Any] | None = None,
        search_queries: list[dict[str, Any]] | None = None,
        enabled: bool = True,
        pag_sig: str = "",
        reason: str = "notebook_created",
        operator: str = "unspecified",
    ) -> dict[str, Any]:
        """Atomically create one complete v4 notebook.

        Enabled notebooks must be bilingual-ready before the single durable
        replace.  Disabled notebooks may be created as incomplete drafts.
        """
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        path = self._path_for(keyword_zh)
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(path.with_suffix(path.suffix + ".lock"))):
            if path.is_file():
                raise FileExistsError(f"notebook already exists: {keyword_zh!r}")
            nb = empty_notebook(keyword_zh)
            nb["enabled"] = False
            if classification is not None:
                nb["classification"] = dict(classification)
            # Build the complete object in memory; no empty intermediate file.
            for raw in search_queries or []:
                query = str(raw.get("query") or "").strip()
                language = str(raw.get("language") or "").strip().lower()
                detected = detect_query_language(query)
                if detected not in {"zh", "en", "mixed"} or language != detected:
                    raise ValueError(f"invalid query/language pair: {query!r}/{language!r}")
                normalized = normalize_keyword(query)
                qid = query_identity(language, normalized)
                if qid in nb["search_queries"]:
                    raise ValueError(f"duplicate normalized query: {query!r}")
                entry = _empty_search_query(
                    query, pag_sig, language=language,
                    source=str(raw.get("source") or "curated"),
                )
                entry["active"] = raw.get("active", True)
                if not isinstance(entry["active"], bool):
                    raise ValueError(f"query active must be boolean: {query!r}")
                nb["search_queries"][qid] = entry
            nb["enabled"] = enabled
            readiness = validate_discovery_readiness(nb)
            if enabled and not readiness:
                raise DiscoveryNotReadyError("cannot create enabled notebook: " + "; ".join(readiness.errors))
            nb["definition_history"].append({
                "at": _now_iso(), "operation": "create", "reason": reason,
                "operator": operator,
                "added_query_ids": sorted(nb["search_queries"]),
                "disabled_query_ids": [], "enabled_query_ids": [],
                "classification_changes": sorted((classification or {}).keys()),
            })
            self._save(path, nb)
            return nb

    def require_v4_ready(self, keyword_zh: str) -> dict[str, Any]:
        """Load a v4 notebook and validate discovery readiness.

        Raises ``DiscoveryNotReadyError`` if the notebook lacks required
        bilingual queries.
        """
        nb = self.require_v4(keyword_zh)
        if nb["enabled"] is False:
            raise DiscoveryNotReadyError(f"notebook {keyword_zh!r} is disabled")
        readiness = validate_discovery_readiness(nb)
        if not readiness:
            raise DiscoveryNotReadyError(
                f"notebook {keyword_zh!r} is not discovery-ready:\n  " +
                "\n  ".join(readiness.errors)
            )
        return nb

    # ── sync search queries (management only) ─────────────────────────

    def sync_search_queries(
        self,
        keyword_zh: str,
        *,
        add: list[dict[str, str]] | None = None,
        disable: list[str] | None = None,
        enable: list[str] | None = None,
        pag_sig: str = "",
        reason: str = "search_query_sync",
        operator: str = "unspecified",
    ) -> dict[str, Any]:
        """Explicitly manage search queries in a v4 notebook.

        ``add``: list of ``{"query": ..., "language": "zh"|"en", "source": ...}``
        dicts.  Existing queries (same language + normalized query) are
        left untouched.  ``source`` defaults to ``"curated"``.

        ``disable`` / ``enable``: lists of query strings to toggle ``active``.

        This is the ONLY path that modifies search query definitions.
        Normal discovery runs never call this method.
        """
        signature = str(pag_sig or "").strip()
        if signature and not _HEX16.fullmatch(signature):
            raise ValueError("pag_sig must be empty or 16 lowercase hex")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-blank string")
        if not isinstance(operator, str) or not operator.strip():
            raise ValueError("operator must be a non-blank string")

        add_ops: dict[str, dict[str, str]] = {}
        for index, raw in enumerate(add or []):
            if not isinstance(raw, dict):
                raise ValueError(f"add[{index}] must be an object")
            query = str(raw.get("query") or "").strip()
            language = str(raw.get("language") or "").strip().lower()
            source = str(raw.get("source") or "curated").strip()
            detected = detect_query_language(query)
            if detected not in {"zh", "en", "mixed"}:
                raise ValueError(f"add[{index}].query is not a valid text query")
            if language not in {"zh", "en", "mixed"}:
                raise ValueError(f"add[{index}].language must be 'zh', 'en', or 'mixed'")
            if language != detected:
                raise ValueError(
                    f"add[{index}] declared language {language!r} does not match {detected!r}"
                )
            if not source:
                raise ValueError(f"add[{index}].source must be non-blank")
            normalized = normalize_keyword(query)
            query_id_value = query_identity(language, normalized)
            candidate = {
                "query": query,
                "language": language,
                "source": source,
            }
            existing_op = add_ops.get(query_id_value)
            if existing_op is not None and (
                existing_op["language"] != language or existing_op["source"] != source
            ):
                raise ValueError(
                    f"add contains duplicate canonical query identity: {query!r}"
                )
            add_ops.setdefault(query_id_value, candidate)

        def normalize_toggles(values: list[str] | None, action: str) -> dict[str, str]:
            operations: dict[str, str] = {}
            for index, raw in enumerate(values or []):
                if not isinstance(raw, str):
                    raise ValueError(f"{action}[{index}] must be a string")
                query = raw.strip()
                language = detect_query_language(query)
                if language not in {"zh", "en", "mixed"}:
                    raise ValueError(
                        f"{action}[{index}] is not a valid Chinese or English query"
                    )
                query_id_value = query_identity(language, normalize_keyword(query))
                operations[query_id_value] = query
            return operations

        disable_ops = normalize_toggles(disable, "disable")
        enable_ops = normalize_toggles(enable, "enable")
        conflicting = set(disable_ops).intersection(enable_ops)
        if conflicting:
            raise ValueError("the same query cannot be enabled and disabled in one batch")
        if set(add_ops).intersection(disable_ops):
            raise ValueError("a query cannot be added/reactivated and disabled in one batch")

        def m(nb: dict[str, Any]) -> None:
            readiness_before = validate_discovery_readiness(nb)
            sq = nb["search_queries"]
            known_ids = set(sq).union(add_ops)
            unknown_disable = sorted(set(disable_ops) - known_ids)
            unknown_enable = sorted(set(enable_ops) - known_ids)
            if unknown_disable or unknown_enable:
                details = []
                if unknown_disable:
                    details.append(
                        "unknown disable queries: "
                        + ", ".join(repr(disable_ops[qid]) for qid in unknown_disable)
                    )
                if unknown_enable:
                    details.append(
                        "unknown enable queries: "
                        + ", ".join(repr(enable_ops[qid]) for qid in unknown_enable)
                    )
                raise ValueError("; ".join(details))

            changes: dict[str, list[str]] = {
                "added": [],
                "reactivated": [],
                "enabled": [],
                "disabled": [],
            }
            now = _now_iso()
            for query_id_value, operation in add_ops.items():
                if query_id_value not in sq:
                    sq[query_id_value] = _empty_search_query(
                        operation["query"],
                        signature,
                        language=operation["language"],
                        source=operation["source"],
                    )
                    changes["added"].append(query_id_value)
                elif sq[query_id_value]["active"] is False:
                    sq[query_id_value]["active"] = True
                    sq[query_id_value]["updated_at"] = now
                    changes["reactivated"].append(query_id_value)
            for query_id_value in disable_ops:
                if sq[query_id_value]["active"] is True:
                    sq[query_id_value]["active"] = False
                    sq[query_id_value]["updated_at"] = now
                    changes["disabled"].append(query_id_value)
            for query_id_value in enable_ops:
                if sq[query_id_value]["active"] is False:
                    sq[query_id_value]["active"] = True
                    sq[query_id_value]["updated_at"] = now
                    changes["enabled"].append(query_id_value)

            if any(changes.values()):
                nb["definition_history"].append({
                    "at": now,
                    "action": "search_queries_updated",
                    "reason": reason.strip(),
                    "operator": operator.strip(),
                    "changes": changes,
                })
            readiness_after = validate_discovery_readiness(nb)
            if nb["enabled"] and not readiness_after.ready:
                raise DiscoveryNotReadyError(
                    "query mutation would make enabled notebook not ready: "
                    + "; ".join(readiness_after.errors)
                )

        return self._mutate(keyword_zh, m)

    # ── active query accessor ─────────────────────────────────────────

    def active_search_queries(self, keyword: str) -> list[dict[str, Any]]:
        """Return all active search queries for a v4 keyword."""
        nb = self.require_v4(keyword)
        return _active_queries(nb)

    # ── refresh state ────────────────────────────────────────────────

    def begin_refresh(self, keyword: str, query_id: str, provider: str) -> None:
        def m(nb: dict[str, Any]) -> None:
            entry = nb["search_queries"].get(query_id)
            if not entry:
                return
            r = entry["providers"].get(provider, {}).get("refresh")
            if r is None:
                return
            r["last_started_at"] = _now_iso()
            r["last_error"] = None
        self._mutate(keyword, m)

    def complete_refresh(
        self, keyword: str, query_id: str, provider: str, *,
        status: str, pages_scanned: int, items_returned: int,
        error: str | None = None,
        window_signature: str | None = None,
        window_pages: int | None = None,
        window_page_ids: list[str] | None = None,
    ) -> None:
        def m(nb: dict[str, Any]) -> None:
            entry = nb["search_queries"].get(query_id)
            if not entry:
                return
            r = entry["providers"].get(provider, {}).get("refresh")
            if r is None:
                return
            r["last_status"] = status
            r["pages_scanned_last_run"] = pages_scanned
            r["items_returned_last_run"] = items_returned
            r["last_error"] = error
            if status in ("success", "partial_success"):
                now = _now_iso()
                r["last_success_at"] = now
                r["consecutive_failures"] = 0
                r["next_retry_at"] = None
                if window_signature is not None:
                    r["last_window_completed_at"] = now
                    r["last_window_signature"] = window_signature
                    if window_pages is not None:
                        r["last_window_pages"] = int(window_pages)
                    if window_page_ids is not None:
                        r["last_window_page_ids"] = list(window_page_ids)
            else:
                r["consecutive_failures"] = int(r.get("consecutive_failures", 0)) + 1
            nb["lifetime_statistics"]["refresh_lane_runs"] = (
                int(nb["lifetime_statistics"].get("refresh_lane_runs", 0)) + 1
            )
            nb["lifetime_statistics"]["provider_items_returned"] = (
                int(nb["lifetime_statistics"].get("provider_items_returned", 0)) + items_returned
            )
        self._mutate(keyword, m)

    # ── backfill state ───────────────────────────────────────────────

    def get_backfill_cursor(self, keyword: str, query_id: str, provider: str) -> str:
        """Return the current backfill cursor (``"*"`` if fresh)."""
        nb = self.require_v4(keyword)
        entry = nb["search_queries"].get(query_id)
        if not entry:
            return INITIAL_CURSOR
        bf = entry["providers"].get(provider, {}).get("backfill", {})
        return bf.get("cursor") or INITIAL_CURSOR

    def get_backfill_state(self, keyword: str, query_id: str, provider: str) -> dict[str, Any]:
        nb = self.require_v4(keyword)
        entry = nb["search_queries"].get(query_id)
        if not entry:
            return {}
        return dict(entry["providers"].get(provider, {}).get("backfill", {}))

    def ensure_backfill_generation(
        self,
        keyword: str,
        query_id: str,
        provider: str,
        *,
        request_signature_hash: str,
    ) -> dict[str, Any]:
        """Bind or roll one provider Backfill state to a request signature.

        A signature change archives the previous generation and resets the
        cursor to ``"*"`` before any provider request can run.  This prevents a
        cursor obtained under one sort/page-size contract from being reused by
        a different provider request contract.
        """
        signature = str(request_signature_hash or "").strip()
        if not _HEX16.fullmatch(signature):
            raise ValueError("request_signature_hash must be 16 lowercase hex")
        result: dict[str, Any] = {}

        def m(nb: dict[str, Any]) -> None:
            nonlocal result
            entry = nb["search_queries"].get(query_id)
            if entry is None:
                raise KeyError(f"unknown query_id: {query_id}")
            if provider not in PROVIDERS:
                raise KeyError(f"unknown provider: {provider}")
            bf = entry["providers"][provider]["backfill"]
            current_signature = bf["request_signature"]
            if current_signature == signature:
                result = dict(bf)
                return

            from src.discovery.backfill_state import (
                BackfillBindDecision,
                BackfillBindError,
                resolve_backfill_generation_binding,
            )
            try:
                decision = resolve_backfill_generation_binding(bf, signature)
            except BackfillBindError as exc:
                raise NotebookCorruptError(str(exc)) from exc

            if decision == BackfillBindDecision.FIRST_BIND:
                bf["request_signature"] = signature
                result = dict(bf)
                return

            # ROLL_GENERATION — current signature differs from requested.
            history = list(bf["generation_history"])
            history.append({
                "generation": int(bf["generation"]),
                "request_signature": current_signature,
                "closed_at": _now_iso(),
                "reason": "request_signature_changed",
                "cursor": bf["cursor"],
                "exhausted": bf["exhausted"],
                "pages_succeeded": bf["pages_succeeded"],
                "pages_committed": bf["pages_committed"],
                "items_returned_total": bf["items_returned_total"],
                "last_committed_page_id": bf["last_committed_page_id"],
            })
            next_generation = max(1, int(bf["generation"]) + 1)
            replacement = _empty_backfill_state(
                signature,
                generation=next_generation,
                generation_history=history,
            )
            entry["providers"][provider]["backfill"] = replacement
            result = dict(replacement)

        self._mutate(keyword, m)
        return result

    def is_backfill_exhausted(self, keyword: str, query_id: str, provider: str) -> bool:
        nb = self.require_v4(keyword)
        entry = nb["search_queries"].get(query_id)
        if not entry:
            return False
        bf = entry["providers"].get(provider, {}).get("backfill", {})
        return bool(bf.get("exhausted"))

    def commit_backfill_cursor(
        self, keyword: str, query_id: str, provider: str, *,
        expected_cursor: str, next_cursor: str | None,
        committed_page_id: str, exhausted: bool,
        items_this_page: int = 0,
        exhaustion_evidence: dict[str, Any] | None = None,
    ) -> "CursorCommitResult":
        # Invariant: exhaustion must carry evidence.  Enforced here, at the
        # only write site, so transient failure paths (which never build
        # evidence) structurally cannot mark a lane exhausted.
        if exhausted and exhaustion_evidence is None:
            raise ValueError(
                "commit_backfill_cursor: exhausted=True requires exhaustion_evidence"
            )
        result: CursorCommitResult | None = None
        conflict_occurred = False
        conflict_msg = ""

        def m(nb: dict[str, Any]) -> None:
            nonlocal result, conflict_occurred, conflict_msg
            entry = nb["search_queries"].get(query_id)
            if not entry:
                conflict_occurred = True
                conflict_msg = f"missing entry for CAS: {query_id}"
                result = CursorCommitResult(committed=False, previous_cursor=expected_cursor,
                                            current_cursor=INITIAL_CURSOR, conflict=True)
                return
            bf = entry["providers"].get(provider, {}).get("backfill")
            if bf is None:
                conflict_occurred = True
                conflict_msg = f"missing provider backfill state: {provider}"
                result = CursorCommitResult(committed=False, previous_cursor=expected_cursor,
                                            current_cursor=INITIAL_CURSOR, conflict=True)
                return
            current = bf.get("cursor") or INITIAL_CURSOR
            if current != expected_cursor:
                bf["cursor_conflicts"] = int(bf.get("cursor_conflicts", 0)) + 1
                conflict_occurred = True
                conflict_msg = (
                    f"cursor conflict for {keyword}/{query_id}/{provider}: "
                    f"expected {expected_cursor!r}, current {current!r}"
                )
                result = CursorCommitResult(committed=False, previous_cursor=expected_cursor,
                                            current_cursor=current, conflict=True)
                return
            if next_cursor is not None:
                bf["cursor"] = next_cursor
            bf["exhausted"] = bool(bf.get("exhausted") or exhausted)
            if exhausted and exhaustion_evidence is not None:
                bf["exhaustion_evidence"] = dict(exhaustion_evidence)
            bf["pages_succeeded"] = int(bf.get("pages_succeeded", 0)) + 1
            bf["pages_committed"] = int(bf.get("pages_committed", 0)) + 1
            bf["items_returned_total"] = int(bf.get("items_returned_total", 0)) + int(items_this_page)
            bf["last_page_count"] = int(items_this_page)
            bf["last_committed_page_id"] = committed_page_id
            bf["last_success_at"] = _now_iso()
            bf["last_error"] = None
            # A successful durable commit clears the backoff schedule.
            bf["consecutive_failures"] = 0
            bf["last_failure_at"] = None
            bf["last_error_type"] = None
            bf["next_retry_at"] = None
            result = CursorCommitResult(committed=True, previous_cursor=expected_cursor,
                                        current_cursor=bf.get("cursor") or INITIAL_CURSOR, conflict=False)

        self._mutate(keyword, m)
        assert result is not None
        if conflict_occurred:
            raise CursorConflictError(conflict_msg)
        return result

    def record_backfill_error(
        self,
        keyword: str,
        query_id: str,
        provider: str,
        *,
        error: str,
        error_type: str | None = None,
        terminal: bool = False,
        backoff_seconds: float | None = None,
    ) -> None:
        """Record a provider/lane failure with the full backoff schedule.

        This is the only write path for the backoff fields
        (``consecutive_failures`` / ``last_failure_at`` / ``last_error_type``
        / ``next_retry_at`` / ``terminal_failure(_at)``), which were
        previously validated but never written.
        """
        def m(nb: dict[str, Any]) -> None:
            entry = nb["search_queries"].get(query_id)
            if not entry:
                return
            bf = entry["providers"].get(provider, {}).get("backfill")
            if bf is None:
                return
            now = _now_iso()
            bf["last_error"] = error
            bf["consecutive_failures"] = int(bf.get("consecutive_failures", 0)) + 1
            bf["last_failure_at"] = now
            bf["last_error_type"] = error_type
            if backoff_seconds is not None:
                from datetime import timedelta
                retry_at = datetime.now(timezone.utc) + timedelta(seconds=float(backoff_seconds))
                bf["next_retry_at"] = retry_at.isoformat()
            if terminal:
                bf["terminal_failure"] = True
                bf["terminal_failure_at"] = now
        self._mutate(keyword, m)

    def record_backfill_run(self, keyword: str, *, items_returned: int) -> None:
        def m(nb: dict[str, Any]) -> None:
            nb["lifetime_statistics"]["backfill_lane_runs"] = (
                int(nb["lifetime_statistics"].get("backfill_lane_runs", 0)) + 1
            )
            nb["lifetime_statistics"]["provider_items_returned"] = (
                int(nb["lifetime_statistics"].get("provider_items_returned", 0)) + items_returned
            )
        self._mutate(keyword, m)

    # ── lifetime statistics ──────────────────────────────────────────

    def record_stage_outcome(self, keyword: str, *, doi_observations: int = 0,
                             new_staged: int = 0, existing_skipped: int = 0) -> None:
        def m(nb: dict[str, Any]) -> None:
            stats = nb["lifetime_statistics"]
            stats["doi_observations"] = int(stats.get("doi_observations", 0)) + doi_observations
            stats["candidates_staged"] = int(stats.get("candidates_staged", 0)) + new_staged
            stats["candidates_existing"] = int(stats.get("candidates_existing", 0)) + existing_skipped
        self._mutate(keyword, m)

    def update_pending_counts(self, keyword: str, *, pages: int, candidates: int) -> None:
        def m(nb: dict[str, Any]) -> None:
            nb["pending"] = {"pages": int(pages), "candidates": int(candidates), "last_drained_at": _now_iso()}
        self._mutate(keyword, m)

    def update_backpressure(self, keyword: str, *, pending_count: int,
                            max_threshold: int, resume_threshold: int) -> dict[str, Any]:
        if resume_threshold < 0 or resume_threshold >= max_threshold:
            raise ValueError("resume_pending_candidates must satisfy 0 <= resume < max")
        result: dict[str, Any] = {}
        def m(nb: dict[str, Any]) -> None:
            nonlocal result
            current = nb.get("backpressure") if isinstance(nb.get("backpressure"), dict) else {}
            active = bool(current.get("active"))
            pending = int(pending_count)
            if active:
                active = pending > resume_threshold
            else:
                active = pending >= max_threshold
            entered_at = current.get("entered_at")
            if active and not current.get("active"):
                entered_at = _now_iso()
            if not active:
                entered_at = None
            result = {"active": active, "entered_at": entered_at, "last_pending_count": pending,
                      "max_threshold": int(max_threshold), "resume_threshold": int(resume_threshold)}
            nb["backpressure"] = result
        self._mutate(keyword, m)
        return result

    # ── management operations ────────────────────────────────────────

    def set_enabled(self, keyword: str, enabled: bool) -> dict[str, Any] | None:
        self.require(keyword)

        def m(nb: dict[str, Any]) -> None:
            requested = bool(enabled)
            if requested:
                nb["enabled"] = True
                readiness = validate_discovery_readiness(nb)
                if not readiness.ready:
                    nb["enabled"] = False
                    raise DiscoveryNotReadyError(
                        "cannot enable an unready notebook: "
                        + "; ".join(readiness.errors)
                    )
            else:
                nb["enabled"] = False

        return self._mutate(keyword, m)

    def set_relevance_profile(
        self,
        keyword: str,
        profile: dict[str, Any],
        *,
        generation: int,
        expected_profile_hash: str | None = None,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        """Atomically bind one validated notebook profile at a generation."""
        from src.discovery.relevance import validate_relevance_profile

        normalized = validate_relevance_profile(profile)
        if isinstance(expected_profile_hash, str) and expected_profile_hash:
            current = self.require_v4(keyword).get("relevance_profile")
            current_hash = current.get("profile_hash") if isinstance(current, dict) else ""
            if current_hash != expected_profile_hash:
                raise CursorConflictError(
                    f"relevance profile changed for {keyword!r}: expected {expected_profile_hash}"
                )

        def m(nb: dict[str, Any]) -> None:
            if isinstance(expected_profile_hash, str) and expected_profile_hash:
                current_profile = nb.get("relevance_profile")
                current_hash = (
                    current_profile.get("profile_hash")
                    if isinstance(current_profile, dict) else ""
                )
                if current_hash != expected_profile_hash:
                    raise CursorConflictError(
                        f"relevance profile changed for {keyword!r}: expected {expected_profile_hash}"
                    )
            if expected_generation is not None and int(nb.get("relevance_generation") or 1) != int(expected_generation):
                raise CursorConflictError(
                    f"relevance generation changed for {keyword!r}: expected {expected_generation}"
                )
            nb["relevance_profile"] = normalized
            nb["relevance_generation"] = int(generation)

        return self._mutate(keyword, m)

    def reset_backfill(self, keyword: str, *, reason: str, pag_sig: str | None = None) -> dict[str, Any] | None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reset reason must be a non-blank string")
        sig = str(pag_sig or "").strip()
        if sig and not _HEX16.fullmatch(sig):
            raise ValueError("pag_sig must be empty or 16 lowercase hex")

        def m(nb: dict[str, Any]) -> None:
            for entry in nb["search_queries"].values():
                for prov in PROVIDERS:
                    bf = entry["providers"][prov]["backfill"]
                    history = list(bf["generation_history"])
                    has_generation = bool(
                        bf["request_signature"]
                        or bf["cursor"] != INITIAL_CURSOR
                        or bf["pages_succeeded"]
                        or bf["pages_committed"]
                        or bf["last_committed_page_id"]
                    )
                    next_generation = int(bf["generation"])
                    if has_generation:
                        history.append({
                            "generation": int(bf["generation"]),
                            "request_signature": bf["request_signature"],
                            "closed_at": _now_iso(),
                            "reason": f"explicit_reset:{reason.strip()}",
                            "cursor": bf["cursor"],
                            "exhausted": bf["exhausted"],
                            "pages_succeeded": bf["pages_succeeded"],
                            "pages_committed": bf["pages_committed"],
                            "items_returned_total": bf["items_returned_total"],
                            "last_committed_page_id": bf["last_committed_page_id"],
                        })
                        next_generation = max(1, next_generation + 1)
                    entry["providers"][prov]["backfill"] = _empty_backfill_state(
                        sig,
                        generation=next_generation if next_generation else None,
                        generation_history=history,
                    )
            nb["reset_history"].append({
                "at": _now_iso(),
                "reason": reason.strip(),
                "scope": "backfill",
            })
        self.require(keyword)
        return self._mutate(keyword, m)

    def list_keywords(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        # Notebook filenames are canonical ``<keyword>__<id>.json``.  The
        # directory may also contain explicitly configured state JSON files;
        # those are not notebook candidates and must not be parsed as such.
        for p in sorted(self.notebook_dir.glob("*__*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise NotebookCorruptError(f"notebook JSON corrupt: {p}: {exc}") from exc
            validate_notebook(data)
            out.append(self._summarize(data))
        return out

    def show(self, keyword: str) -> dict[str, Any] | None:
        nb = self.load(keyword)
        if nb is None:
            return None
        return self._summarize(nb)

    @staticmethod
    def _summarize(data: dict[str, Any]) -> dict[str, Any]:
        validate_notebook(data)
        kw_zh = data["keyword_zh"]
        queries_list = list(data["search_queries"].values())
        active = [e for e in queries_list if e["active"]]
        readiness = validate_discovery_readiness(data)
        return {
            "keyword_zh": kw_zh,
            "keyword_id": data["keyword_id"],
            "enabled": data["enabled"],
            "ready": readiness.ready,
            "active_queries": len(active),
            "queries": [
                {
                    "query_id": e.get("query_id", ""),
                    "query": e.get("query", ""),
                    "language": e.get("language", ""),
                    "active": e.get("active", False),
                    "source": e.get("source", ""),
                    "providers": {
                        prov: {
                            "refresh_status": e.get("providers", {}).get(prov, {}).get("refresh", {}).get("last_status"),
                            "backfill_cursor": e.get("providers", {}).get(prov, {}).get("backfill", {}).get("cursor"),
                            "backfill_exhausted": e.get("providers", {}).get(prov, {}).get("backfill", {}).get("exhausted"),
                            "backfill_pages": e.get("providers", {}).get(prov, {}).get("backfill", {}).get("pages_succeeded"),
                        }
                        for prov in PROVIDERS
                    },
                }
                for e in queries_list
            ],
        }


@dataclass(frozen=True)
class CursorCommitResult:
    committed: bool
    previous_cursor: str
    current_cursor: str
    conflict: bool

