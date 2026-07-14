#!/usr/bin/env python
"""Read-only audit for the active discovery notebook v3 closure.

The audit is deliberately stricter than the runtime loaders.  It validates
notebooks, provider generations, durable page journals, provenance records,
and the Catalog registry as one identity graph.  It never repairs, rewrites,
or moves a source file.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (  # noqa: E402
    CATALOG_FOLDER_ROOT,
    CATALOG_STATE_ROOT,
    DISCOVERY_DIR,
    DISCOVERY_EXPORTS_DIR,
    DISCOVERY_KEYWORD_NOTEBOOK_DIR,
    DISCOVERY_LOCKS_DIR,
    DISCOVERY_PENDING_PAGES_DIR,
)
from src.catalog_folders.registry_schema import validate_registry_schema  # noqa: E402
from src.discovery.backfill_state import (  # noqa: E402
    REASON_CURSOR_ADVANCED,
    REASON_CURSOR_CONFLICTS,
    REASON_EXHAUSTED,
    REASON_GENERATION_HISTORY,
    REASON_ITEMS_RETURNED,
    REASON_LAST_COMMITTED_PAGE,
    REASON_LAST_ERROR,
    REASON_LAST_FAILURE,
    REASON_LAST_PAGE_COUNT,
    REASON_LAST_SUCCESS,
    REASON_PAGES_COMMITTED,
    REASON_PAGES_SUCCEEDED,
    REASON_TERMINAL_FAILURE,
    describe_nonpristine_unbound_backfill,
    is_strictly_pristine_unbound_backfill,
)
from src.discovery.constants import INITIAL_CURSOR  # noqa: E402
from src.discovery.keyword_notebook import (  # noqa: E402
    PROVIDERS,
    SCHEMA_VERSION,
    detect_query_language,
    keyword_id,
    notebook_filename,
    query_identity,
    validate_discovery_readiness,
    validate_notebook,
)
from src.discovery.page_journal import validate_page  # noqa: E402


PROVENANCE_FIELDS = frozenset({"keyword_id", "query_id", "provider", "lane"})
LANES = frozenset({"refresh", "backfill"})
COMMITTED_PAGE_STATES = frozenset({"cursor_committed", "draining", "drained"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _relative(path: Path, base: Path = PROJECT_ROOT) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_read_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _json_files(root: Path, *, recursive: bool = True) -> list[Path]:
    if not root.is_dir():
        return []
    iterator = root.rglob("*.json") if recursive else root.glob("*.json")
    return sorted(path for path in iterator if path.is_file())


def _error(
    errors: list[dict[str, Any]],
    kind: str,
    path: Path | str,
    message: str,
    **fields: Any,
) -> None:
    row = {"kind": kind, "path": str(path), "message": message}
    row.update(fields)
    errors.append(row)


def _warning(
    warnings: list[dict[str, Any]],
    kind: str,
    path: Path | str,
    message: str,
    **fields: Any,
) -> None:
    row = {"kind": kind, "path": str(path), "message": message}
    row.update(fields)
    warnings.append(row)


def _notebook_row(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    raw, read_error = _safe_read_json(path)
    if read_error:
        return None, read_error
    if not isinstance(raw, dict):
        return None, "JSON root must be an object"
    try:
        validate_notebook(raw)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return raw, None


def scan_active_notebooks(notebook_dir: Path) -> dict[str, dict[str, Any]]:
    """Return validated v3 notebooks keyed by ``keyword_id``.

    ``__errors__`` is retained for callers that want a lightweight scanner
    result; :func:`run_audit` adds structured diagnostics and never depends on
    a scanner silently dropping an invalid file.
    """
    result: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    for path in _json_files(Path(notebook_dir), recursive=False):
        data, error = _notebook_row(path)
        if error:
            errors.append({"path": str(path), "message": error})
            continue
        assert data is not None
        kid = str(data["keyword_id"])
        row = dict(data)
        row["__path__"] = str(path)
        row["__sha256__"] = _sha256(path)
        if kid in result:
            result[kid].setdefault("__duplicate_paths__", []).append(str(path))
        result[kid] = row
    if errors:
        result["__errors__"] = {str(index): row for index, row in enumerate(errors)}
    return result


def scan_retired_notebooks(retired_root: Path) -> dict[str, dict[str, Any]]:
    """Return validated retired v3 notebooks without treating them as active."""
    result: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    for path in _json_files(Path(retired_root)):
        data, error = _notebook_row(path)
        if error:
            errors.append({"path": str(path), "message": error})
            continue
        assert data is not None
        kid = str(data["keyword_id"])
        row = dict(data)
        row["__path__"] = str(path)
        row["__sha256__"] = _sha256(path)
        if kid in result:
            result[kid].setdefault("__duplicate_paths__", []).append(str(path))
        result[kid] = row
    if errors:
        result["__errors__"] = {str(index): row for index, row in enumerate(errors)}
    return result


def scan_pending_pages(pending_dir: Path) -> list[dict[str, Any]]:
    """Read pending page journals from the strict v3 path layout."""
    result: list[dict[str, Any]] = []
    root = Path(pending_dir)
    for path in _json_files(root):
        data, read_error = _safe_read_json(path)
        row: dict[str, Any] = {"__path__": str(path)}
        if read_error:
            row["__error__"] = read_error
        elif not isinstance(data, dict):
            row["__error__"] = "JSON root must be an object"
        else:
            row.update(data)
            try:
                validate_page(data, path)
            except Exception as exc:
                row["__error__"] = f"{type(exc).__name__}: {exc}"
        result.append(row)
    return result


def scan_locks(locks_dir: Path) -> dict[str, list[str]]:
    """List transient discovery locks using the v3 identity path."""
    root = Path(locks_dir)
    result: dict[str, list[str]] = {
        "state_locks": [],
        "doi_locks": [],
        "resolution_locks": [],
        "unknown": [],
    }
    if not root.is_dir():
        return result
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(root).parts
        value = path.relative_to(root).as_posix()
        if rel and rel[0] in {"doi", "resolution"}:
            result[f"{rel[0]}_locks"].append(value)
        elif len(rel) == 3 and rel[2].endswith(".backfill.lock"):
            result["state_locks"].append(value)
        else:
            result["unknown"].append(value)
    return result


def _load_registry(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not path.is_file():
        return {"schema_version": "1.0", "categories": []}, []
    data, error = _safe_read_json(path)
    if error:
        return {}, [{"kind": "registry_schema", "path": str(path), "message": error}]
    if not isinstance(data, dict):
        return {}, [{
            "kind": "registry_schema", "path": str(path),
            "message": "registry JSON root must be an object",
        }]
    errors = [
        {"kind": "registry_schema", "path": str(path), "message": message}
        for message in validate_registry_schema(data)
    ]
    return data, errors


def _scan_provenance_files(
    roots: Iterable[Path],
    *,
    notebooks: dict[str, dict[str, Any]],
    errors: list[dict[str, Any]],
) -> int:
    """Validate optional query provenance without using query text as identity."""
    seen: set[Path] = set()
    observations = 0

    def visit(value: Any, path: Path, location: str) -> None:
        nonlocal observations
        if isinstance(value, dict):
            if "query_id" in value or "lane" in value:
                observations += 1
                missing = sorted(PROVENANCE_FIELDS - set(value))
                if missing:
                    _error(
                        errors,
                        "receipt_provenance",
                        path,
                        f"provenance object missing {missing}",
                        location=location,
                    )
                else:
                    kid = str(value.get("keyword_id") or "")
                    qid = str(value.get("query_id") or "")
                    provider = str(value.get("provider") or "")
                    lane = str(value.get("lane") or "")
                    if kid not in notebooks:
                        _error(errors, "receipt_provenance", path, "unknown keyword_id", keyword_id=kid, location=location)
                    elif qid not in notebooks[kid].get("search_queries", {}):
                        _error(errors, "receipt_provenance", path, "unknown query_id", keyword_id=kid, query_id=qid, location=location)
                    if provider not in PROVIDERS:
                        _error(errors, "receipt_provenance", path, "unknown provider", provider=provider, location=location)
                    if lane not in LANES:
                        _error(errors, "receipt_provenance", path, "unknown lane", lane=lane, location=location)
            for key, child in value.items():
                visit(child, path, f"{location}.{key}" if location else str(key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, path, f"{location}[{index}]")

    for root in roots:
        if root.is_file():
            candidate_files = [root]
        else:
            candidate_files = sorted(
                path for pattern in ("*.json", "*.jsonl")
                for path in root.rglob(pattern)
                if path.is_file()
            ) if root.is_dir() else []
        for path in candidate_files:
            if path in seen:
                continue
            seen.add(path)
            if path.suffix.lower() == ".jsonl":
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except OSError as exc:
                    _error(errors, "receipt_provenance", path, str(exc))
                    continue
                for index, line in enumerate(lines, 1):
                    if not line.strip():
                        continue
                    try:
                        visit(json.loads(line), path, f"line[{index}]")
                    except json.JSONDecodeError as exc:
                        _error(errors, "receipt_provenance", path, f"JSONL parse error at line {index}: {exc}")
                continue
            data, error = _safe_read_json(path)
            if error:
                _error(errors, "receipt_provenance", path, error)
                continue
            visit(data, path, "")
    return observations


def _page_path_identity(path: Path, root: Path) -> tuple[str, str, str, str, str] | None:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return None
    if len(parts) != 5 or not parts[-1].endswith(".json"):
        return None
    return parts[0], parts[1], parts[2], parts[3], Path(parts[4]).stem


def _page_signature(page: dict[str, Any]) -> str:
    signature = page.get("request_signature")
    if isinstance(signature, dict):
        return str(signature.get("hash") or "")
    return str(signature or "")


def _check_page_chain(
    pages: list[dict[str, Any]],
    state: dict[str, Any],
    *,
    notebook_path: str,
    errors: list[dict[str, Any]],
    identity: dict[str, Any],
    compare_state: bool = True,
) -> None:
    """Check one generation's opaque cursor chain.

    ``pages`` must already be partitioned by keyword/query/provider/lane,
    generation, and request-signature.  A restart at ``*`` is therefore a
    new chain, not a branch in an earlier generation.
    """
    if not pages:
        if compare_state and (
            int(state.get("pages_committed") or 0)
            or state.get("last_committed_page_id")
        ):
            _error(
                errors,
                "backfill_state",
                notebook_path,
                "progress has no corresponding page journal",
                **identity,
            )
        return
    by_request: dict[str, list[dict[str, Any]]] = defaultdict(list)
    page_ids = {str(row["page_id"]): row for row in pages}
    for page in pages:
        request_cursor = str(page.get("request_cursor") or INITIAL_CURSOR)
        by_request[request_cursor].append(page)
        if page.get("request_cursor") == page.get("next_cursor") and not page.get("provider_exhausted"):
            _error(
                errors,
                "page_journal",
                page.get("__path__", ""),
                "cursor edge does not advance",
                **identity,
            )
    for cursor, rows in by_request.items():
        next_values = {row.get("next_cursor") for row in rows}
        if len(rows) > 1:
            _error(
                errors,
                "page_journal",
                rows[0].get("__path__", ""),
                "multiple page journals share one opaque request cursor",
                **identity,
                request_cursor=cursor,
                page_ids=[row.get("page_id") for row in rows],
            )
        if len(next_values) > 1:
            _error(
                errors,
                "page_journal",
                rows[0].get("__path__", ""),
                "divergent journal edges for one opaque request cursor",
                **identity,
                request_cursor=cursor,
                page_ids=[row.get("page_id") for row in rows],
            )

    # Walk the only allowed predecessor/successor proof from the initial
    # cursor.  No lexical, numeric, or length ordering is meaningful for
    # provider cursors.
    reachable: list[dict[str, Any]] = []
    cursor = INITIAL_CURSOR
    visited: set[str] = set()
    while cursor in by_request:
        if cursor in visited:
            _error(
                errors,
                "page_journal",
                by_request[cursor][0].get("__path__", ""),
                "cursor chain contains a cycle",
                **identity,
                cursor=cursor,
            )
            break
        visited.add(cursor)
        rows = by_request[cursor]
        if len(rows) != 1:
            break
        page = rows[0]
        reachable.append(page)
        if page.get("provider_exhausted") or page.get("next_cursor") is None:
            break
        cursor = str(page["next_cursor"])
    if len(reachable) != len(pages):
        _error(
            errors,
            "page_journal",
            next((row.get("__path__", "") for row in pages if row not in reachable), ""),
            "page journal chain is incomplete or contains a disconnected branch",
            **identity,
            page_ids=[row.get("page_id") for row in pages if row not in reachable],
        )

    if not compare_state:
        return

    # A page is journaled before the cursor CAS.  A fetched tail is therefore
    # valid evidence of an interrupted transaction, but it is not counted as
    # committed progress in the notebook state.
    committed: list[dict[str, Any]] = []
    saw_uncommitted = False
    for page in reachable:
        if page.get("state") in COMMITTED_PAGE_STATES:
            if saw_uncommitted:
                _error(
                    errors,
                    "page_journal",
                    page.get("__path__", ""),
                    "committed journal follows an uncommitted page",
                    **identity,
                )
            committed.append(page)
        else:
            saw_uncommitted = True
    expected_committed = len(committed)
    for field in ("pages_succeeded", "pages_committed"):
        observed = int(state.get(field) or 0)
        if observed != expected_committed:
            _error(
                errors,
                "backfill_state",
                notebook_path,
                f"{field} does not match the provable committed chain length",
                **identity,
                field=field,
                observed=observed,
                expected=expected_committed,
            )

    current_cursor = str(state.get("cursor") or INITIAL_CURSOR)
    expected_cursor = INITIAL_CURSOR
    if committed:
        last_committed = committed[-1]
        expected_cursor = str(
            last_committed.get("request_cursor") or INITIAL_CURSOR
            if last_committed.get("provider_exhausted") or last_committed.get("next_cursor") is None
            else last_committed.get("next_cursor")
        )
    elif reachable:
        expected_cursor = str(reachable[0].get("request_cursor") or INITIAL_CURSOR)
    if current_cursor != expected_cursor:
        _error(
            errors,
            "backfill_state",
            notebook_path,
            "current cursor does not follow the committed journal prefix",
            **identity,
            cursor=current_cursor,
            expected_cursor=expected_cursor,
        )

    last_page = str(state.get("last_committed_page_id") or "")
    expected_last_page = str(committed[-1]["page_id"]) if committed else ""
    if last_page != expected_last_page:
        _error(
            errors,
            "backfill_state",
            notebook_path,
            "last_committed_page_id does not match the committed journal prefix",
            **identity,
            page_id=last_page,
            expected_page_id=expected_last_page,
        )

    if state.get("exhausted"):
        if not committed or not committed[-1].get("provider_exhausted"):
            _error(
                errors,
                "backfill_state",
                notebook_path,
                "exhausted state has no committed terminal journal",
                **identity,
            )
    elif committed and committed[-1].get("provider_exhausted"):
        _error(
            errors,
                "backfill_state",
            notebook_path,
            "terminal journal is not reflected in exhausted state",
            **identity,
        )


def _check_backfill_states(
    notebooks: dict[str, dict[str, Any]],
    page_rows: list[dict[str, Any]],
    *,
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> dict[str, int]:
    pages_by_identity: dict[tuple[str, str, str, str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for page in page_rows:
        if page.get("__error__"):
            continue
        try:
            generation = int(page.get("generation") or 0)
        except (TypeError, ValueError):
            generation = 0
        pages_by_identity[
            (
                str(page.get("keyword_id") or ""),
                str(page.get("query_id") or ""),
                str(page.get("provider") or ""),
                str(page.get("lane") or ""),
                generation,
                _page_signature(page),
            )
        ].append(page)

    generations_scanned = 0
    historical_generations = 0
    current_generations = 0
    pristine_unbound_lanes = 0

    for kid, notebook in notebooks.items():
        notebook_path = str(notebook.get("__path__", ""))
        for qid, query_entry in notebook.get("search_queries", {}).items():
            for provider in PROVIDERS:
                backfill = query_entry["providers"][provider]["backfill"]
                generation = int(backfill.get("generation") or 0)
                if generation < 1:
                    _error(errors, "generation", notebook_path, "generation must be at least 1", keyword_id=kid, query_id=qid, provider=provider)
                signature = str(backfill.get("request_signature") or "")
                history = backfill.get("generation_history") or []
                history_generations = [int(row.get("generation") or 0) for row in history]
                history_by_generation = {
                    int(row.get("generation") or 0): row for row in history
                }
                if len(history_generations) != len(set(history_generations)):
                    _error(errors, "generation", notebook_path, "generation history contains duplicates", keyword_id=kid, query_id=qid, provider=provider)
                if generation in history_generations:
                    _error(errors, "generation", notebook_path, "active generation also appears in history", keyword_id=kid, query_id=qid, provider=provider)

                # Build page groups BEFORE the signature check so that a page
                # journal on disk is treated as durable progress.  Without this
                # ordering, a notebook whose state looks pristine (empty cursor,
                # zero counters) but already has a committed page journal would
                # be misclassified as a pristine unbound lane.
                page_groups = {
                    key: rows
                    for key, rows in pages_by_identity.items()
                    if key[:4] == (kid, qid, provider, "backfill")
                }
                generations_scanned += len(page_groups)
                has_page_journal = any(key[4] == generation for key in page_groups)

                DURABLE_REASON_CODES = frozenset({
                    REASON_CURSOR_ADVANCED, REASON_EXHAUSTED,
                    REASON_PAGES_SUCCEEDED, REASON_PAGES_COMMITTED,
                    REASON_ITEMS_RETURNED, REASON_LAST_PAGE_COUNT,
                    REASON_LAST_COMMITTED_PAGE, REASON_CURSOR_CONFLICTS,
                    REASON_LAST_SUCCESS,
                })
                state_pristine = is_strictly_pristine_unbound_backfill(backfill)
                if not signature:
                    if has_page_journal or not state_pristine:
                        reasons = describe_nonpristine_unbound_backfill(backfill)
                        has_durable = any(r in DURABLE_REASON_CODES for r in reasons)
                        has_terminal = REASON_TERMINAL_FAILURE in reasons
                        has_history = REASON_GENERATION_HISTORY in reasons
                        if has_page_journal or has_durable or has_terminal or has_history:
                            _error(errors, "generation", notebook_path,
                                   "non-pristine state without request signature",
                                   keyword_id=kid, query_id=qid, provider=provider,
                                   reasons=list(reasons))
                        else:
                            # Only transient failure/retry fields — no durable
                            # progress, no terminal, no history.
                            _warning(warnings, "generation", notebook_path,
                                     "failure state without request signature",
                                     keyword_id=kid, query_id=qid, provider=provider,
                                     reasons=list(reasons))
                    else:
                        pristine_unbound_lanes += 1

                current_group_found = False
                for key, page_group in sorted(page_groups.items()):
                    _, _, _, _, page_generation, page_signature = key
                    identity = {
                        "keyword_id": kid,
                        "query_id": qid,
                        "provider": provider,
                        "lane": "backfill",
                        "generation": page_generation,
                        "request_signature": page_signature,
                    }
                    if page_generation == generation:
                        current_generations += 1
                        current_group_found = True
                        if page_signature != signature:
                            _error(
                                errors,
                                "page_journal",
                                page_group[0].get("__path__", ""),
                                "current generation page signature differs from notebook state",
                                **identity,
                                expected_signature=signature,
                            )
                            continue
                        _check_page_chain(
                            page_group,
                            backfill,
                            notebook_path=notebook_path,
                            errors=errors,
                            identity=identity,
                        )
                        continue
                    historical_generations += 1
                    history_row = history_by_generation.get(page_generation)
                    if history_row is None:
                        _error(
                            errors,
                            "generation",
                            page_group[0].get("__path__", ""),
                            "page generation is not in notebook history",
                            **identity,
                        )
                        continue
                    expected_signature = str(history_row.get("request_signature") or "")
                    if page_signature != expected_signature:
                        _error(
                            errors,
                            "page_journal",
                            page_group[0].get("__path__", ""),
                            "historical generation page signature differs from history",
                            **identity,
                            expected_signature=expected_signature,
                        )
                        continue
                    _check_page_chain(
                        page_group,
                        history_row,
                        notebook_path=notebook_path,
                        errors=errors,
                        identity=identity,
                        compare_state=any(
                            field in history_row
                            for field in ("cursor", "pages_succeeded", "pages_committed", "last_committed_page_id", "exhausted")
                        ),
                    )

                if not current_group_found:
                    _check_page_chain(
                        [],
                        backfill,
                        notebook_path=notebook_path,
                        errors=errors,
                        identity={
                            "keyword_id": kid,
                            "query_id": qid,
                            "provider": provider,
                            "lane": "backfill",
                            "generation": generation,
                            "request_signature": signature,
                        },
                    )

    return {
        "generations_scanned": generations_scanned,
        "historical_generations": historical_generations,
        "current_generations": current_generations,
        "pristine_unbound_lanes": pristine_unbound_lanes,
    }


def _check_page_identity(
    page_rows: list[dict[str, Any]],
    notebooks: dict[str, dict[str, Any]],
    *,
    pending_root: Path,
    errors: list[dict[str, Any]],
) -> None:
    for row in page_rows:
        path = Path(str(row.get("__path__", "")))
        if row.get("__error__"):
            _error(errors, "page_journal", path, str(row["__error__"]))
            continue
        identity = _page_path_identity(path, pending_root)
        if identity is None:
            _error(errors, "page_journal", path, "page path must be keyword_id/query_id/provider/lane/page_id.json")
            continue
        kid, qid, provider, lane, page_id = identity
        for field, expected in (
            ("keyword_id", kid), ("query_id", qid), ("provider", provider),
            ("lane", lane), ("page_id", page_id),
        ):
            if str(row.get(field) or "") != expected:
                _error(errors, "page_journal", path, f"{field} does not match path", field=field, expected=expected)
        notebook = notebooks.get(kid)
        if notebook is None:
            _error(errors, "page_journal", path, "orphan notebook identity", keyword_id=kid, query_id=qid)
            continue
        query_entry = notebook.get("search_queries", {}).get(qid)
        if query_entry is None:
            _error(errors, "page_journal", path, "orphan query identity", keyword_id=kid, query_id=qid)
            continue
        if row.get("query") != query_entry.get("query") or row.get("query_language") != query_entry.get("language"):
            _error(errors, "page_journal", path, "page query provenance differs from notebook", keyword_id=kid, query_id=qid)
        if provider not in PROVIDERS or lane not in LANES:
            _error(errors, "page_journal", path, "unknown provider or lane", provider=provider, lane=lane)


def _active_categories(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("category_id")): row
        for row in registry.get("categories", [])
        if isinstance(row, dict) and not row.get("retired_at") and row.get("classification_enabled", True)
    }


def run_audit() -> dict[str, Any]:
    """Run a complete strict-v3 audit without mutating any path."""
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    notebook_rows: dict[str, dict[str, Any]] = {}
    disabled_drafts = 0
    ready_notebooks = 0
    enabled_notebooks = 0
    notebook_files = _json_files(DISCOVERY_KEYWORD_NOTEBOOK_DIR, recursive=False)
    for path in notebook_files:
        data, error = _notebook_row(path)
        if error:
            _error(errors, "notebook_schema", path, error)
            continue
        assert data is not None
        kid = str(data["keyword_id"])
        if kid in notebook_rows:
            _error(errors, "notebook_identity", path, "duplicate keyword_id", keyword_id=kid)
        if path.name != notebook_filename(str(data["keyword_zh"])):
            _error(errors, "notebook_identity", path, "filename is not derived from keyword_zh", keyword_id=kid)
        row = dict(data)
        row["__path__"] = str(path)
        row["__sha256__"] = _sha256(path)
        notebook_rows[kid] = row
        if data["enabled"]:
            enabled_notebooks += 1
            readiness = validate_discovery_readiness(data)
            if readiness.ready:
                ready_notebooks += 1
            else:
                _error(errors, "readiness", path, "enabled notebook is not bilingual-ready", keyword_id=kid, details=readiness.errors)
        else:
            disabled_drafts += 1
            _warning(warnings, "disabled_draft", path, "disabled notebook is allowed to be not ready", keyword_id=kid)

    retired_rows = scan_retired_notebooks(DISCOVERY_KEYWORD_NOTEBOOK_DIR.parent / "keyword_notebooks_retired")
    for key, row in retired_rows.items():
        if key.startswith("__"):
            continue
        if key in notebook_rows:
            _error(errors, "notebook_identity", row.get("__path__", ""), "active and retired notebook share keyword_id", keyword_id=key)

    page_rows = scan_pending_pages(DISCOVERY_PENDING_PAGES_DIR)
    _check_page_identity(page_rows, notebook_rows, pending_root=DISCOVERY_PENDING_PAGES_DIR, errors=errors)
    generation_stats = _check_backfill_states(
        notebook_rows, page_rows, errors=errors, warnings=warnings,
    )

    registry_path = CATALOG_STATE_ROOT / "category_registry.json"
    registry, registry_errors = _load_registry(registry_path)
    errors.extend(registry_errors)
    categories = _active_categories(registry)
    for kid, notebook in notebook_rows.items():
        if not notebook["enabled"]:
            continue
        category = categories.get(kid)
        if category is None:
            _error(errors, "registry", registry_path, "enabled notebook missing active category", keyword_id=kid)
        elif category.get("keyword_zh") != notebook["keyword_zh"] or category.get("directory_name") != notebook["keyword_zh"]:
            _error(errors, "registry", registry_path, "category identity differs from notebook keyword_zh", keyword_id=kid)
    for kid, category in categories.items():
        if kid not in notebook_rows or not notebook_rows[kid]["enabled"]:
            _error(errors, "registry", registry_path, "active category has no enabled notebook", keyword_id=kid)

    category_dirs: list[str] = []
    if CATALOG_FOLDER_ROOT.is_dir():
        reserved = {"all", "_pending", ".state"}
        for path in sorted(CATALOG_FOLDER_ROOT.iterdir()):
            if not path.is_dir() or path.name in reserved:
                continue
            category_dirs.append(path.name)
            if path.name not in {str(row.get("directory_name")) for row in categories.values()}:
                _error(errors, "catalog_category", path, "category directory is not an active registry category")
            if detect_query_language(path.name) not in {"zh", "mixed"}:
                _error(errors, "catalog_category", path, "category directory is not Chinese")
    for category in categories.values():
        if category.get("directory_name") not in category_dirs:
            _warning(warnings, "catalog_category", CATALOG_FOLDER_ROOT, "active category directory is missing", keyword_id=category.get("category_id"))

    locks = scan_locks(DISCOVERY_LOCKS_DIR)
    for lock in locks["state_locks"]:
        parts = Path(lock).parts
        if len(parts) == 3:
            kid, qid, filename = parts
            if kid not in notebook_rows or qid not in notebook_rows.get(kid, {}).get("search_queries", {}):
                _error(errors, "lock", lock, "state lock references unknown query identity", keyword_id=kid, query_id=qid)
            if filename != "openalex.backfill.lock" and filename != "crossref.backfill.lock":
                _error(errors, "lock", lock, "unknown provider lock")
    for lock in locks["unknown"]:
        _warning(warnings, "lock", lock, "unrecognized transient lock path")

    provenance_roots = [DISCOVERY_EXPORTS_DIR, DISCOVERY_DIR / "doi_candidates"]
    provenance_count = _scan_provenance_files(provenance_roots, notebooks=notebook_rows, errors=errors)

    schema_errors = sum(row["kind"] == "notebook_schema" for row in errors)
    generation_errors = sum(row["kind"] == "generation" for row in errors)
    journal_errors = sum(row["kind"] == "page_journal" for row in errors)
    receipt_errors = sum(row["kind"] == "receipt_provenance" for row in errors)
    notebook_schema_safe = schema_errors == 0 and all(row.get("kind") != "notebook_identity" for row in errors)
    page_journal_safe = journal_errors == 0
    backfill_state_safe = generation_errors == 0 and not any(row["kind"] == "backfill_state" for row in errors)
    receipt_provenance_safe = receipt_errors == 0
    discovery_query_ready = enabled_notebooks == ready_notebooks
    migration_safe = notebook_schema_safe and page_journal_safe and backfill_state_safe and receipt_provenance_safe and not any(
        row["kind"] in {"notebook_identity", "registry", "catalog_category", "lock"} for row in errors
    )
    identities = []
    for kid, notebook in sorted(notebook_rows.items()):
        identities.append({
            "keyword_id": kid,
            "keyword_zh": notebook["keyword_zh"],
            "enabled": notebook["enabled"],
            "ready": bool(validate_discovery_readiness(notebook).ready) if notebook["enabled"] else False,
            "notebook_path": notebook["__path__"],
            "query_ids": sorted(notebook["search_queries"]),
            "page_count": sum(1 for row in page_rows if row.get("keyword_id") == kid),
            "category_directory": next((row.get("directory_name") for row in categories.values() if row.get("category_id") == kid), None),
        })

    return {
        "audit_schema_version": "1.0",
        "audit_timestamp": _now_iso(),
        "notebook_schema_safe": notebook_schema_safe,
        "discovery_query_ready": discovery_query_ready,
        "backfill_state_safe": backfill_state_safe,
        "page_journal_safe": page_journal_safe,
        "receipt_provenance_safe": receipt_provenance_safe,
        "migration_safe": migration_safe,
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "v3_notebooks_scanned": len(notebook_files),
            "ready_notebooks": ready_notebooks,
            "enabled_notebooks": enabled_notebooks,
            "disabled_drafts": disabled_drafts,
            "schema_errors": schema_errors,
            "generation_errors": generation_errors,
            "journal_errors": journal_errors,
            "receipt_errors": receipt_errors,
            "page_journals_scanned": len(page_rows),
            **generation_stats,
            "provenance_observations": provenance_count,
            "active_categories": len(categories),
            "category_directories": len(category_dirs),
        },
        "identities": identities,
        "sources_scanned": {
            "notebooks": _relative(DISCOVERY_KEYWORD_NOTEBOOK_DIR),
            "pending_pages": _relative(DISCOVERY_PENDING_PAGES_DIR),
            "locks": _relative(DISCOVERY_LOCKS_DIR),
            "exports": _relative(DISCOVERY_EXPORTS_DIR),
            "doi_candidates": _relative(DISCOVERY_DIR / "doi_candidates"),
            "registry": _relative(registry_path),
            "catalog_root": _relative(CATALOG_FOLDER_ROOT),
        },
    }


def generate_markdown_report(report: dict[str, Any]) -> str:
    """Render an audit report without reading or writing repository state."""
    summary = report.get("summary", {})
    lines = [
        "# Discovery Keyword Notebook v3 Audit",
        "",
        f"Audit timestamp: `{report.get('audit_timestamp', '')}`",
        "",
        "| Safety field | Result |",
        "|---|---|",
    ]
    for field in (
        "notebook_schema_safe", "discovery_query_ready", "backfill_state_safe",
        "page_journal_safe", "receipt_provenance_safe", "migration_safe",
    ):
        lines.append(f"| `{field}` | `{bool(report.get(field))}` |")
    lines.extend([
        "",
        f"- v3 notebooks scanned: {summary.get('v3_notebooks_scanned', 0)}",
        f"- ready notebooks: {summary.get('ready_notebooks', 0)}",
        f"- disabled drafts: {summary.get('disabled_drafts', 0)}",
        f"- schema errors: {summary.get('schema_errors', 0)}",
        f"- generation errors: {summary.get('generation_errors', 0)}",
        f"- journal errors: {summary.get('journal_errors', 0)}",
        f"- pristine unbound lanes (never activated): {summary.get('pristine_unbound_lanes', 0)}",
        "",
        "## Errors",
        "",
    ])
    if report.get("errors"):
        for item in report["errors"]:
            lines.append(f"- `{item.get('kind')}` `{item.get('path')}`: {item.get('message')}")
    else:
        lines.append("- none")
    lines.extend(["", "## Warnings", ""])
    if report.get("warnings"):
        for item in report["warnings"]:
            lines.append(f"- `{item.get('kind')}` `{item.get('path')}`: {item.get('message')}")
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only strict v3 discovery audit.")
    parser.add_argument("--json", action="store_true", help="Emit JSON to stdout.")
    parser.add_argument("--output-dir", type=Path, default=None, help="Explicitly write a report pair here.")
    args = parser.parse_args(argv)
    report = run_audit()
    if args.output_dir is not None:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        (args.output_dir / f"discovery_keyword_v3_audit_{stamp}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        (args.output_dir / f"discovery_keyword_v3_audit_{stamp}.md").write_text(
            generate_markdown_report(report), encoding="utf-8",
        )
    if args.json or args.output_dir is None:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(generate_markdown_report(report))
    return 0 if not report["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
