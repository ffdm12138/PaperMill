"""Per-keyword discovery notebook: tracks Refresh/Backfill progress.

Each original keyword gets one JSON notebook file under
``DISCOVERY_KEYWORD_NOTEBOOK_DIR``. The notebook stores, per expanded
query and per provider (openalex/crossref), Refresh statistics and the
Backfill cursor so that:

- Refresh always restarts from page 1 (cursor ``"*"``).
- Backfill resumes from the last saved cursor.
- Adding a new expansion does not reset existing progress.
- Refresh updates never overwrite Backfill cursors (field-level merge).
- Removing an expansion marks it ``active=False`` but preserves history.

Identity: the notebook for a keyword is uniquely identified by the
SHA-256 of the *identity key* (NFC-normalized, whitespace-folded,
casefolded keyword). The filename also carries a human-readable slug,
but uniqueness is enforced by the 16-hex ``keyword_id`` — two keywords
that differ only in case or surrounding whitespace map to the same
notebook.

Concurrency: each notebook file has a companion ``.lock`` (via
``filelock``). All updates read-modify-write inside the lock and only
merge the touched lane/provider/expansion node, so concurrent Refresh
and Backfill lanes for the same keyword cannot clobber each other. The
write itself uses an inline tmp+``os.replace`` (NOT
``atomic_write_json``) so we do not re-acquire the same lock file we
already hold. The same per-keyword lock also serializes backfill cursor
advancement, so two threads cannot consume the same cursor concurrently.

Corrupt JSON fails closed: ``load()`` raises ``NotebookCorruptError`` so
callers can surface a hard failure rather than silently re-initializing
and losing cursors.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from filelock import FileLock

from src.utils.atomic_io import _fsync_dir as _fsync_dir_if_posix


SCHEMA_VERSION = "2.0"
LEGACY_SCHEMA_VERSION = "1.0"
PAGINATION_SCHEMA_VERSION = "2.0"
INITIAL_CURSOR = "*"

# Lane / provider literals (kept as plain strings for JSON readability).
LANES = ("refresh", "backfill")
PROVIDERS = ("openalex", "crossref")


class NotebookCorruptError(RuntimeError):
    """Raised when a notebook file cannot be parsed as valid JSON dict."""


class LegacyNotebookError(RuntimeError):
    """Raised by active discovery when a v1 notebook would be unsafe to use."""


class CursorConflictError(RuntimeError):
    """Raised when expected-cursor CAS detects a stale writer."""


# ── Keyword normalization & identity ─────────────────────────────────


def normalize_keyword(keyword: str) -> str:
    """Strip + Unicode NFC + fold runs of whitespace.

    Case is NOT folded here (CJK has no case; English case is folded
    only at identity-comparison time via ``_identity_key``).
    """
    if not keyword:
        return ""
    value = unicodedata.normalize("NFC", keyword.strip())
    return re.sub(r"\s+", " ", value)


def _identity_key(normalized: str) -> str:
    """Casefolded key used for uniqueness / hashing."""
    return re.sub(r"\s+", " ", normalized).casefold().strip()


def keyword_id(keyword: str) -> str:
    """Stable 16-hex id from the normalized+casefolded keyword."""
    normalized = normalize_keyword(keyword)
    return hashlib.sha256(_identity_key(normalized).encode("utf-8")).hexdigest()[:16]


def keyword_fingerprint8(keyword: str) -> str:
    return keyword_id(keyword)[:8]


def safe_slug(keyword: str, max_len: int = 48) -> str:
    """Human-readable filename component (NOT the identity)."""
    s = re.sub(r"[^\w一-鿿]+", "_", keyword).strip("_")
    s = re.sub(r"_+", "_", s)
    return (s or "query")[:max_len].rstrip("_")


def notebook_filename(keyword: str) -> str:
    """``<safe_slug>__<fp8>.json`` — slug is cosmetic, fp8 is the identity."""
    return f"{safe_slug(keyword)}__{keyword_fingerprint8(keyword)}.json"


def notebook_path(keyword: str, notebook_dir: Path) -> Path:
    return Path(notebook_dir) / notebook_filename(keyword)


def resolve_existing_notebook(
    keyword: str, notebook_dir: Path
) -> Path | None:
    """Find the notebook for ``keyword`` if it already exists.

    Scans the directory for a file whose ``keyword_id`` matches. This
    handles the rare case where the slug changed (e.g. keyword edited in
    place) but the identity is unchanged.
    """
    target_id = keyword_id(keyword)
    nb_dir = Path(notebook_dir)
    if not nb_dir.is_dir():
        return None
    # Fast path: the canonical filename exists.
    canonical = notebook_path(keyword, nb_dir)
    if canonical.is_file():
        return canonical
    # Slow path: scan for a matching keyword_id in the JSON content.
    for p in sorted(nb_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict) and data.get("keyword_id") == target_id:
            return p
    return None


# ── Pagination signature ─────────────────────────────────────────────


def pagination_signature(
    sort: str | None = None,
    page_size: int | None = None,
    schema_version: str = PAGINATION_SCHEMA_VERSION,
) -> str:
    """Hash of sort + page size + pagination schema.

    If sort or page size change semantically, the signature changes and a NEW
    backfill state is created (the old one is preserved, marked inactive).

    Provider filters are NOT part of this signature: the project does not yet
    plumb filters through CLI → DiscoveryOptions → provider request, so
    accepting them here would let a caller believe filters affect the cursor
    when they silently do not. When filters are added end-to-end, they must be
    added here AND in ``composite_backfill_signature`` simultaneously.
    """
    parts = [str(sort or ""), str(page_size or ""), schema_version]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def composite_backfill_signature(
    *,
    page_size: int,
    openalex_backfill_sort: str | None = None,
    crossref_backfill_sort: str | None = None,
    schema_version: str = PAGINATION_SCHEMA_VERSION,
) -> str:
    """Expansion-level Backfill generation signature.

    This intentionally treats the expansion as one generation. If either
    provider's Backfill paging identity changes, both provider cursors restart
    from a new inactive/active expansion boundary. Refresh options do not belong
    here because they never advance the Backfill cursor.

    Only fields that actually reach the provider request are part of the
    signature: page size, pagination schema version, and each provider's
    Backfill sort. Provider filters are deliberately omitted because no
    CLI/config/DiscoveryOptions/provider-request path supplies them today;
    keeping a permanently-empty ``backfill_filters`` field would be a
    half-implemented contract. Adding filters later requires wiring them
    through CLI, DiscoveryOptions, the provider request, the request
    signature, this composite signature, and tests in one change.
    """
    payload = {
        "page_size": int(page_size),
        "pagination_schema_version": schema_version,
        "providers": {
            "openalex": {"backfill_sort": openalex_backfill_sort or ""},
            "crossref": {"backfill_sort": crossref_backfill_sort or ""},
        },
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def expansion_key(expanded_query: str, pag_sig: str) -> str:
    """Stable key for an expansion entry: hash(query | pag_sig)."""
    q = normalize_keyword(expanded_query)
    return hashlib.sha256(f"{q}|{pag_sig}".encode("utf-8")).hexdigest()[:16]


# ── Notebook state factories ─────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_refresh_state() -> dict[str, Any]:
    return {
        "last_started_at": None,
        "last_success_at": None,
        "last_status": None,
        "pages_scanned_last_run": 0,
        "items_returned_last_run": 0,
        "last_error": None,
    }


def _empty_backfill_state(pag_sig: str) -> dict[str, Any]:
    return {
        "cursor": INITIAL_CURSOR,
        "exhausted": False,
        "pages_succeeded": 0,
        "pages_committed": 0,
        "items_returned_total": 0,
        "last_page_count": 0,
        "last_committed_page_id": "",
        "cursor_conflicts": 0,
        "last_success_at": None,
        "last_error": None,
        "pagination_signature": pag_sig,
        "request_signature": pag_sig,
        # Backoff / terminal-failure tracking (backward-compatible defaults
        # are added by _migrate_backfill_state for old notebooks).
        "consecutive_failures": 0,
        "last_failure_at": None,
        "last_error_type": None,
        "next_retry_at": None,
        "terminal_failure": False,
        "terminal_failure_at": None,
    }


def _empty_provider_state(pag_sig: str) -> dict[str, Any]:
    return {
        "refresh": _empty_refresh_state(),
        "backfill": _empty_backfill_state(pag_sig),
    }


_BACKOFF_DEFAULTS: dict[str, Any] = {
    "consecutive_failures": 0,
    "last_failure_at": None,
    "last_error_type": None,
    "next_retry_at": None,
    "terminal_failure": False,
    "terminal_failure_at": None,
}


def _migrate_backfill_state(nb: dict[str, Any]) -> dict[str, Any]:
    """Backfill new state fields into old notebook entries (backward compat).

    Old notebooks created before the backoff/terminal-failure fields were
    introduced will not have them.  This adds ``setdefault`` for each new
    field so the rest of the code can assume they exist.  Does NOT create
    a new schema version — the additions are purely additive defaults.
    """
    for exp in nb.get("expansions", {}).values():
        providers = exp.get("providers", {})
        for prov_state in providers.values():
            bf = prov_state.get("backfill")
            if isinstance(bf, dict):
                for key, default in _BACKOFF_DEFAULTS.items():
                    bf.setdefault(key, default)
    return nb


def _empty_expansion(query: str, pag_sig: str) -> dict[str, Any]:
    return {
        "query": query,
        "active": True,
        "created_at": _now_iso(),
        "providers": {
            "openalex": _empty_provider_state(pag_sig),
            "crossref": _empty_provider_state(pag_sig),
        },
    }


def empty_notebook(keyword: str) -> dict[str, Any]:
    """Build a fresh notebook dict for a new keyword."""
    normalized = normalize_keyword(keyword)
    return {
        "schema_version": SCHEMA_VERSION,
        "keyword_id": keyword_id(keyword),
        "keyword": keyword.strip(),
        "normalized_keyword": normalized,
        "enabled": True,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "expansions": {},
        "lifetime_statistics": {
            "keyword_runs": 0,
            "refresh_lane_runs": 0,
            "backfill_lane_runs": 0,
            "provider_page_attempts": 0,
            "provider_page_successes": 0,
            "provider_page_failures": 0,
            "provider_items_returned": 0,
            "doi_observations": 0,
            "candidates_staged": 0,
            "candidates_existing": 0,
        },
        "pending": {"pages": 0, "candidates": 0, "last_drained_at": None},
        "backpressure": {
            "active": False,
            "entered_at": None,
            "last_pending_count": 0,
            "max_threshold": 1000,
            "resume_threshold": 700,
        },
        "reset_history": [],
        "migration_history": [],
    }


def _validate_notebook(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise NotebookCorruptError(f"notebook root is {type(data).__name__}, expected dict")
    for key in ("schema_version", "keyword_id", "expansions", "lifetime_statistics"):
        if key not in data:
            raise NotebookCorruptError(f"notebook missing key: {key}")
    if not isinstance(data["expansions"], dict):
        raise NotebookCorruptError("notebook.expansions is not a dict")
    return data


def assert_active_schema(data: dict[str, Any]) -> None:
    version = str(data.get("schema_version") or "")
    if version == LEGACY_SCHEMA_VERSION:
        raise LegacyNotebookError(
            "discovery notebook schema v1 has unsafe cursors; run "
            "scripts/migrate_discovery_notebooks_v2.py before active discovery"
        )
    if version != SCHEMA_VERSION:
        raise NotebookCorruptError(f"unsupported notebook schema_version: {version}")


# ── Store ────────────────────────────────────────────────────────────


@dataclass
class LaneRunResult:
    """Summary of one lane run for the per-keyword report."""

    lane: str
    status: str
    pages: int
    items_returned: int
    provider_failures: int
    exhausted_states: int


class KeywordNotebookStore:
    """File-backed store with per-keyword locking and field-level merge."""

    def __init__(self, notebook_dir: Path | str):
        self.notebook_dir = Path(notebook_dir)
        self.notebook_dir.mkdir(parents=True, exist_ok=True)

    # ── path / lock resolution ───────────────────────────────────────

    def _path_for(self, keyword: str) -> Path:
        existing = resolve_existing_notebook(keyword, self.notebook_dir)
        return existing if existing is not None else notebook_path(keyword, self.notebook_dir)

    def _lock_for(self, keyword: str) -> FileLock:
        nb_path = self._path_for(keyword)
        return FileLock(str(nb_path.with_suffix(nb_path.suffix + ".lock")))

    # ── load / save ──────────────────────────────────────────────────

    def load(self, keyword: str) -> dict[str, Any] | None:
        """Load notebook or return None if absent. Corrupt → raises.

        Acquires the per-keyword lock so concurrent writes (``os.replace``)
        in the other lane cannot race with this read on Windows.
        """
        path = self._path_for(keyword)
        if not path.is_file():
            return None
        with self._lock_for(keyword):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise NotebookCorruptError(f"notebook JSON corrupt: {path}: {exc}") from exc
            return _migrate_backfill_state(_validate_notebook(data))

    def load_active(self, keyword: str) -> dict[str, Any] | None:
        nb = self.load(keyword)
        if nb is not None:
            assert_active_schema(nb)
        return nb

    def _read_or_init(self, path: Path, keyword: str) -> dict[str, Any]:
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise NotebookCorruptError(
                    f"notebook JSON corrupt: {path}: {exc}"
                ) from exc
            nb = _validate_notebook(data)
            assert_active_schema(nb)
            # Backfill new backoff/terminal fields for old notebooks.
            nb = _migrate_backfill_state(nb)
            return nb
        return empty_notebook(keyword)

    def _save(self, path: Path, nb: dict[str, Any]) -> None:
        """Write inline (tmp + os.replace + fsync) — caller already holds the lock.

        Retries ``os.replace`` a few times to handle transient Windows
        ``PermissionError`` when AV/indexing briefly holds the target.
        Performs ``flush`` + ``os.fsync`` on the tmp file before replace,
        and best-effort parent-directory fsync on POSIX.
        """
        nb["updated_at"] = _now_iso()
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
                # Last resort: re-raise.
                if last_exc:
                    try:
                        os.replace(tmp, path)
                    except Exception:
                        raise last_exc
            # Best-effort parent directory fsync (POSIX only).
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
        with FileLock(str(path.with_suffix(path.suffix + ".lock"))):
            nb = self._read_or_init(path, keyword)
            mutator(nb)
            self._save(path, nb)
            return nb

    # ── expansion management ─────────────────────────────────────────

    def ensure_keyword(
        self,
        keyword: str,
        expansion_queries: Iterable[str],
        pag_sig: str,
    ) -> dict[str, Any]:
        """Get or create the notebook, seeding any missing expansions.

        Existing expansions keep their cursors. An expansion whose
        pagination signature changed gets a NEW backfill state (the old
        one is preserved under a stale key). Expansions not present in
        ``expansion_queries`` this run are marked ``active=False`` but
        NOT deleted. The ``enabled`` flag is NOT touched here — use
        ``set_enabled`` to manage it.
        """
        wanted_keys: set[str] = set()

        def m(nb: dict[str, Any]) -> None:
            for q in expansion_queries:
                ekey = expansion_key(q, pag_sig)
                wanted_keys.add(ekey)
                if ekey not in nb["expansions"]:
                    nb["expansions"][ekey] = _empty_expansion(q, pag_sig)
                else:
                    exp = nb["expansions"][ekey]
                    exp["active"] = True
                    # If pagination signature changed, reset backfill
                    # state (cursor invalidated by new sort/filters).
                    for prov in PROVIDERS:
                        bf = exp["providers"].get(prov, {}).get("backfill", {})
                        if bf.get("pagination_signature") and \
                                bf["pagination_signature"] != pag_sig:
                            exp["providers"][prov]["backfill"] = _empty_backfill_state(pag_sig)
            # Mark absent expansions inactive (preserve history).
            for ekey, exp in nb["expansions"].items():
                if ekey not in wanted_keys:
                    exp["active"] = False

        return self._mutate(keyword, m)

    def require(self, keyword: str) -> dict[str, Any]:
        nb = self.load(keyword)
        if nb is None:
            raise FileNotFoundError(f"no notebook for keyword: {keyword!r}")
        assert_active_schema(nb)
        return nb

    # ── refresh state ────────────────────────────────────────────────

    def begin_refresh(
        self, keyword: str, ekey: str, provider: str
    ) -> None:
        def m(nb: dict[str, Any]) -> None:
            exp = nb["expansions"].get(ekey)
            if not exp:
                return
            r = exp["providers"].get(provider, {}).get("refresh")
            if r is None:
                return
            r["last_started_at"] = _now_iso()
            r["last_error"] = None
        self._mutate(keyword, m)

    def complete_refresh(
        self,
        keyword: str,
        ekey: str,
        provider: str,
        *,
        status: str,
        pages_scanned: int,
        items_returned: int,
        error: str | None = None,
    ) -> None:
        def m(nb: dict[str, Any]) -> None:
            exp = nb["expansions"].get(ekey)
            if not exp:
                return
            r = exp["providers"].get(provider, {}).get("refresh")
            if r is None:
                return
            r["last_status"] = status
            r["pages_scanned_last_run"] = pages_scanned
            r["items_returned_last_run"] = items_returned
            r["last_error"] = error
            if status in ("success", "partial_success"):
                r["last_success_at"] = _now_iso()
            nb["lifetime_statistics"]["refresh_lane_runs"] = (
                int(nb["lifetime_statistics"].get("refresh_lane_runs", 0)) + 1
            )
            nb["lifetime_statistics"]["provider_items_returned"] = (
                int(nb["lifetime_statistics"].get("provider_items_returned", 0)) + items_returned
            )
        self._mutate(keyword, m)

    # ── backfill state ───────────────────────────────────────────────

    def get_backfill_cursor(
        self, keyword: str, ekey: str, provider: str
    ) -> str:
        """Return the current backfill cursor (``"*"`` if fresh)."""
        nb = self.load_active(keyword)
        if nb is None:
            return INITIAL_CURSOR
        exp = nb["expansions"].get(ekey)
        if not exp:
            return INITIAL_CURSOR
        bf = exp["providers"].get(provider, {}).get("backfill", {})
        return bf.get("cursor") or INITIAL_CURSOR

    def get_backfill_state(
        self, keyword: str, ekey: str, provider: str
    ) -> dict[str, Any]:
        nb = self.load_active(keyword)
        if nb is None:
            return {}
        exp = nb["expansions"].get(ekey)
        if not exp:
            return {}
        return dict(exp["providers"].get(provider, {}).get("backfill", {}))

    def is_backfill_exhausted(
        self, keyword: str, ekey: str, provider: str
    ) -> bool:
        nb = self.load_active(keyword)
        if nb is None:
            return False
        exp = nb["expansions"].get(ekey)
        if not exp:
            return False
        bf = exp["providers"].get(provider, {}).get("backfill", {})
        return bool(bf.get("exhausted"))

    def advance_backfill(
        self,
        keyword: str,
        ekey: str,
        provider: str,
        *,
        next_cursor: str | None,
        items_this_page: int,
        exhausted: bool = False,
    ) -> None:
        """Advance the backfill cursor on a successful page.

        Only called on genuine success. If ``next_cursor`` is None and
        ``exhausted`` is False, the cursor is left unchanged (the page
        returned results but offered no next cursor — treat as exhausted
        only if the provider explicitly signaled end-of-results).
        """
        def m(nb: dict[str, Any]) -> None:
            exp = nb["expansions"].get(ekey)
            if not exp:
                return
            bf = exp["providers"].get(provider, {}).get("backfill")
            if bf is None:
                return
            if next_cursor is not None:
                bf["cursor"] = next_cursor
            bf["exhausted"] = bool(bf.get("exhausted") or exhausted)
            bf["pages_succeeded"] = int(bf.get("pages_succeeded", 0)) + 1
            bf["items_returned_total"] = (
                int(bf.get("items_returned_total", 0)) + items_this_page
            )
            bf["last_page_count"] = items_this_page
            bf["last_success_at"] = _now_iso()
            bf["last_error"] = None
        self._mutate(keyword, m)

    def commit_backfill_cursor(
        self,
        keyword: str,
        ekey: str,
        provider: str,
        *,
        expected_cursor: str,
        next_cursor: str | None,
        committed_page_id: str,
        exhausted: bool,
        items_this_page: int = 0,
    ) -> "CursorCommitResult":
        """Commit a backfill cursor with expected-cursor CAS.

        On cursor conflict, ``cursor_conflicts`` is incremented and
        **persisted** before ``CursorConflictError`` is raised.  The
        mutator returns normally so that ``_save()`` runs; the exception
        is raised afterwards by the outer method.
        """
        result: CursorCommitResult | None = None
        conflict_occurred = False
        conflict_msg = ""

        def m(nb: dict[str, Any]) -> None:
            nonlocal result, conflict_occurred, conflict_msg
            exp = nb["expansions"].get(ekey)
            if not exp:
                conflict_occurred = True
                conflict_msg = f"missing expansion for CAS: {ekey}"
                result = CursorCommitResult(
                    committed=False,
                    previous_cursor=expected_cursor,
                    current_cursor=INITIAL_CURSOR,
                    conflict=True,
                )
                return
            bf = exp["providers"].get(provider, {}).get("backfill")
            if bf is None:
                conflict_occurred = True
                conflict_msg = f"missing provider backfill state: {provider}"
                result = CursorCommitResult(
                    committed=False,
                    previous_cursor=expected_cursor,
                    current_cursor=INITIAL_CURSOR,
                    conflict=True,
                )
                return
            current = bf.get("cursor") or INITIAL_CURSOR
            if current != expected_cursor:
                # PERSIST the counter BEFORE we bail out: the mutator
                # returns normally so _save() runs, then the outer
                # method raises CursorConflictError.
                bf["cursor_conflicts"] = int(bf.get("cursor_conflicts", 0)) + 1
                conflict_occurred = True
                conflict_msg = (
                    f"cursor conflict for {keyword}/{ekey}/{provider}: "
                    f"expected {expected_cursor!r}, current {current!r}"
                )
                result = CursorCommitResult(
                    committed=False,
                    previous_cursor=expected_cursor,
                    current_cursor=current,
                    conflict=True,
                )
                return
            if next_cursor is not None:
                bf["cursor"] = next_cursor
            bf["exhausted"] = bool(bf.get("exhausted") or exhausted)
            bf["pages_succeeded"] = int(bf.get("pages_succeeded", 0)) + 1
            bf["pages_committed"] = int(bf.get("pages_committed", 0)) + 1
            bf["items_returned_total"] = int(bf.get("items_returned_total", 0)) + int(items_this_page)
            bf["last_page_count"] = int(items_this_page)
            bf["last_committed_page_id"] = committed_page_id
            bf["last_success_at"] = _now_iso()
            bf["last_error"] = None
            result = CursorCommitResult(
                committed=True,
                previous_cursor=expected_cursor,
                current_cursor=bf.get("cursor") or INITIAL_CURSOR,
                conflict=False,
            )

        self._mutate(keyword, m)
        assert result is not None
        if conflict_occurred:
            raise CursorConflictError(conflict_msg)
        return result

    def record_backfill_error(
        self,
        keyword: str,
        ekey: str,
        provider: str,
        *,
        error: str,
    ) -> None:
        """Record a backfill failure WITHOUT advancing the cursor."""
        def m(nb: dict[str, Any]) -> None:
            exp = nb["expansions"].get(ekey)
            if not exp:
                return
            bf = exp["providers"].get(provider, {}).get("backfill")
            if bf is None:
                return
            bf["last_error"] = error
        self._mutate(keyword, m)

    def record_backfill_run(
        self, keyword: str, *, items_returned: int
    ) -> None:
        """Bump lifetime backfill stats (called once per keyword run)."""
        def m(nb: dict[str, Any]) -> None:
            nb["lifetime_statistics"]["backfill_lane_runs"] = (
                int(nb["lifetime_statistics"].get("backfill_lane_runs", 0)) + 1
            )
            nb["lifetime_statistics"]["provider_items_returned"] = (
                int(nb["lifetime_statistics"].get("provider_items_returned", 0)) + items_returned
            )
        self._mutate(keyword, m)

    # ── lifetime statistics ──────────────────────────────────────────

    def record_stage_outcome(
        self,
        keyword: str,
        *,
        doi_observations: int = 0,
        new_staged: int = 0,
        existing_skipped: int = 0,
    ) -> None:
        def m(nb: dict[str, Any]) -> None:
            stats = nb["lifetime_statistics"]
            stats["doi_observations"] = int(stats.get("doi_observations", 0)) + doi_observations
            stats["candidates_staged"] = int(stats.get("candidates_staged", 0)) + new_staged
            stats["candidates_existing"] = int(stats.get("candidates_existing", 0)) + existing_skipped
        self._mutate(keyword, m)

    def update_pending_counts(self, keyword: str, *, pages: int, candidates: int) -> None:
        def m(nb: dict[str, Any]) -> None:
            nb["pending"] = {
                "pages": int(pages),
                "candidates": int(candidates),
                "last_drained_at": _now_iso(),
            }
        self._mutate(keyword, m)

    def update_backpressure(
        self,
        keyword: str,
        *,
        pending_count: int,
        max_threshold: int,
        resume_threshold: int,
    ) -> dict[str, Any]:
        """Persist keyword-level pending backpressure with hysteresis."""
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
            result = {
                "active": active,
                "entered_at": entered_at,
                "last_pending_count": pending,
                "max_threshold": int(max_threshold),
                "resume_threshold": int(resume_threshold),
            }
            nb["backpressure"] = result

        self._mutate(keyword, m)
        return result

    # ── management operations ────────────────────────────────────────

    def set_enabled(self, keyword: str, enabled: bool) -> dict[str, Any] | None:
        self.require(keyword)
        return self._mutate(keyword, lambda nb: nb.__setitem__("enabled", bool(enabled)))

    def reset_backfill(
        self, keyword: str, *, reason: str, pag_sig: str | None = None
    ) -> dict[str, Any] | None:
        """Reset all backfill cursors for ONE keyword.

        Does NOT delete paper_raw, DOIs, or affect other keywords. The
        reset is recorded in ``reset_history``.
        """
        sig = pag_sig or pagination_signature()

        def m(nb: dict[str, Any]) -> None:
            for ekey, exp in nb["expansions"].items():
                for prov in PROVIDERS:
                    exp["providers"][prov]["backfill"] = _empty_backfill_state(sig)
            nb.setdefault("reset_history", []).append({
                "at": _now_iso(),
                "reason": reason,
                "scope": "backfill",
            })
        self.require(keyword)
        return self._mutate(keyword, m)

    def list_keywords(self) -> list[dict[str, Any]]:
        """Return a summary list of all notebooks in the store.

        Reads each file without a per-keyword lock (this is a management
        scan, not run concurrently with discovery). Transient read errors
        (e.g. a file being replaced on Windows) are skipped.
        """
        out: list[dict[str, Any]] = []
        for p in sorted(self.notebook_dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict):
                continue
            out.append(self._summarize(data, p))
        return out

    def show(self, keyword: str) -> dict[str, Any] | None:
        nb = self.load(keyword)
        if nb is None:
            return None
        return self._summarize(nb, self._path_for(keyword))

    @staticmethod
    def _summarize(data: dict[str, Any], path: Path) -> dict[str, Any]:
        expansions = data.get("expansions", {})
        active = [e for e in expansions.values() if e.get("active")]
        return {
            "keyword": data.get("keyword", ""),
            "keyword_id": data.get("keyword_id", ""),
            "enabled": data.get("enabled", True),
            "path": str(path),
            "updated_at": data.get("updated_at"),
            "active_expansions": len(active),
            "expansions": [
                {
                    "query": e.get("query", ""),
                    "active": e.get("active", False),
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
                for e in expansions.values()
            ],
            "lifetime_statistics": data.get("lifetime_statistics", {}),
        }


@dataclass(frozen=True)
class CursorCommitResult:
    committed: bool
    previous_cursor: str
    current_cursor: str
    conflict: bool
