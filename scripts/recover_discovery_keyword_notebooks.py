#!/usr/bin/env python
"""Inspect-only recovery planner for strict discovery notebook v3.

Recovery writes are intentionally unavailable until a plan-bound, byte-checked
transaction implementation is completed.  This command therefore performs a
read-only proof pass and emits proposed operations with source hashes.  It
never changes notebooks, journals, locks, or a transaction directory.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (  # noqa: E402
    CATALOG_FOLDER_ROOT,
    DISCOVERY_KEYWORD_NOTEBOOK_DIR,
    DISCOVERY_LOCKS_DIR,
    DISCOVERY_PENDING_PAGES_DIR,
)
from src.discovery.constants import INITIAL_CURSOR  # noqa: E402
from src.discovery.keyword_notebook import (  # noqa: E402
    PROVIDERS,
    validate_notebook,
)
from src.discovery.page_journal import validate_page  # noqa: E402


class RecoveryApplyUnavailable(RuntimeError):
    """Raised if a caller attempts to turn the inspect-only tool into a writer."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _relative(path: Path, base: Path) -> str:
    return path.resolve().relative_to(base.resolve()).as_posix()


def _json_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*.json") if path.is_file())


def _read_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _issue(
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


def _load_notebooks(
    root: Path,
    *,
    errors: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    notebooks: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("*.json")) if root.is_dir() else []:
        data, read_error = _read_json(path)
        if read_error:
            _issue(errors, "notebook_schema", path, read_error)
            continue
        if not isinstance(data, dict):
            _issue(errors, "notebook_schema", path, "notebook JSON root must be an object")
            continue
        try:
            validate_notebook(data)
        except Exception as exc:
            _issue(errors, "notebook_schema", path, f"{type(exc).__name__}: {exc}")
            continue
        kid = str(data["keyword_id"])
        if kid in notebooks:
            _issue(errors, "notebook_identity", path, "duplicate keyword_id", keyword_id=kid)
        row = deepcopy(data)
        row["__path__"] = str(path)
        row["__sha256__"] = _sha256_file(path)
        notebooks[kid] = row
    return notebooks


def _load_pages(
    root: Path,
    *,
    errors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    for path in _json_files(root):
        data, read_error = _read_json(path)
        if read_error:
            _issue(errors, "page_journal", path, read_error)
            continue
        if not isinstance(data, dict):
            _issue(errors, "page_journal", path, "page JSON root must be an object")
            continue
        try:
            validate_page(data, path)
        except Exception as exc:
            _issue(errors, "page_journal", path, f"{type(exc).__name__}: {exc}")
            continue
        row = deepcopy(data)
        row["__path__"] = str(path)
        row["__sha256__"] = _sha256_file(path)
        pages.append(row)
    return pages


def _edge_chain(
    pages: list[dict[str, Any]],
    *,
    errors: list[dict[str, Any]],
    identity: dict[str, str],
) -> list[dict[str, Any]] | None:
    """Return a proven opaque-cursor chain, or ``None`` on divergence."""
    by_request: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for page in pages:
        request_cursor = str(page.get("request_cursor") or INITIAL_CURSOR)
        by_request[request_cursor].append(page)
    for request_cursor, rows in by_request.items():
        next_values = {row.get("next_cursor") for row in rows}
        if len(next_values) != 1:
            _issue(
                errors,
                "cursor_divergence",
                rows[0].get("__path__", ""),
                "multiple journal successors exist for one opaque cursor",
                **identity,
                request_cursor=request_cursor,
                journal_ids=[row.get("page_id") for row in rows],
            )
            return None
        if next_values == {request_cursor} and not rows[0].get("provider_exhausted"):
            _issue(errors, "cursor_divergence", rows[0].get("__path__", ""), "journal cursor edge does not advance", **identity)
            return None
    chain: list[dict[str, Any]] = []
    cursor = INITIAL_CURSOR
    visited: set[str] = set()
    while cursor in by_request:
        if cursor in visited:
            _issue(errors, "cursor_divergence", by_request[cursor][0].get("__path__", ""), "journal chain contains a cycle", **identity)
            return None
        visited.add(cursor)
        page = by_request[cursor][0]
        chain.append(page)
        next_cursor = page.get("next_cursor")
        if not next_cursor or page.get("provider_exhausted"):
            break
        cursor = str(next_cursor)
    if len(chain) != len(pages):
        orphaned = [page for page in pages if page not in chain]
        _issue(
            errors,
            "cursor_divergence",
            orphaned[0].get("__path__", "") if orphaned else "",
            "journal chain is incomplete or has an unproven branch",
            **identity,
            journal_ids=[page.get("page_id") for page in orphaned],
        )
        return None
    return chain


def _state_operation(
    notebook: dict[str, Any],
    pages: list[dict[str, Any]],
    *,
    notebook_path: Path,
    base: Path,
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    generation: int,
    expected_signature: str,
    current_generation: int,
    history_state: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not pages:
        return None
    kid = str(notebook["keyword_id"])
    qid = str(pages[0]["query_id"])
    provider = str(pages[0]["provider"])
    identity = {
        "keyword_id": kid,
        "query_id": qid,
        "provider": provider,
        "lane": "backfill",
        "generation": generation,
        "request_signature": expected_signature,
    }
    state = notebook["search_queries"].get(qid, {}).get("providers", {}).get(provider, {}).get("backfill")
    if not isinstance(state, dict):
        _issue(errors, "query_identity", notebook_path, "page query has no notebook backfill state", **identity)
        return None
    if not expected_signature:
        _issue(errors, "signature", notebook_path, "journal generation has no request signature", **identity)
        return None
    mismatched = [
        page for page in pages
        if str(page.get("request_signature", {}).get("hash") or "") != expected_signature
    ]
    if mismatched:
        _issue(
            errors,
            "signature",
            mismatched[0].get("__path__", ""),
            "journal signature differs from notebook generation",
            **identity,
        )
        return None
    chain = _edge_chain(pages, errors=errors, identity=identity)
    if chain is None:
        return None

    source_journals = [
        _relative(Path(page["__path__"]), base) for page in chain
    ]
    if generation != current_generation:
        history = history_state or {}
        if int(history.get("pages_committed") or 0) > len(chain):
            _issue(
                errors,
                "cursor_divergence",
                notebook_path,
                "historical pages_committed exceeds the proven journal chain",
                **identity,
            )
            return None
        historical_page_id = str(history.get("last_committed_page_id") or "")
        if historical_page_id and historical_page_id not in {str(page["page_id"]) for page in chain}:
            _issue(
                errors,
                "cursor_divergence",
                notebook_path,
                "historical last_committed_page_id is outside the proven chain",
                **identity,
                page_id=historical_page_id,
            )
            return None
        _warning(
            warnings,
            "historical_generation",
            notebook_path,
            "historical generation is complete and will not be restored",
            **identity,
            recoverable=False,
            source_journals=source_journals,
        )
        return None

    if generation != int(state.get("generation") or 0):
        _issue(
            errors,
            "generation",
            notebook_path,
            "journal is not the notebook's current generation",
            **identity,
            current_generation=int(state.get("generation") or 0),
        )
        return None
    current_cursor = str(state.get("cursor") or INITIAL_CURSOR)
    terminal = chain[-1]
    terminal_cursor = str(terminal.get("next_cursor") or terminal.get("request_cursor") or INITIAL_CURSOR)

    # Prove the recovery suffix from the notebook's actual cursor.  The
    # notebook state, not the largest generation number or a lexical cursor
    # comparison, determines whether this is the current recoverable chain.
    boundaries: dict[str, int] = {INITIAL_CURSOR: 0}
    for index, page in enumerate(chain):
        request_cursor = str(page.get("request_cursor") or INITIAL_CURSOR)
        boundaries.setdefault(request_cursor, index)
        next_cursor = page.get("next_cursor")
        after = str(
            page.get("request_cursor") or INITIAL_CURSOR
            if page.get("provider_exhausted") or next_cursor is None
            else next_cursor
        )
        boundaries[after] = index + 1
    if current_cursor not in boundaries:
        _issue(
            errors,
            "cursor_divergence",
            notebook_path,
            "notebook cursor is not explained by the current generation journal chain",
            **identity,
            cursor=current_cursor,
        )
        return None
    if int(state.get("pages_committed") or 0) > len(chain):
        _issue(
            errors,
            "cursor_divergence",
            notebook_path,
            "notebook has more committed pages than the journal chain",
            **identity,
        )
        return None
    last_id = str(state.get("last_committed_page_id") or "")
    if last_id and last_id not in {str(page["page_id"]) for page in chain}:
        _issue(
            errors,
            "cursor_divergence",
            notebook_path,
            "last committed journal is outside the proven chain",
            **identity,
            page_id=last_id,
        )
        return None
    # The internal path/hash annotations belong to the inspection result, not
    # to the notebook payload that a future plan-bound writer would install.
    desired = {
        key: deepcopy(value)
        for key, value in notebook.items()
        if not str(key).startswith("__")
    }
    desired_state = desired["search_queries"][qid]["providers"][provider]["backfill"]
    desired_state["cursor"] = terminal_cursor
    desired_state["exhausted"] = bool(terminal.get("provider_exhausted"))
    desired_state["pages_succeeded"] = max(int(desired_state.get("pages_succeeded") or 0), len(chain))
    desired_state["pages_committed"] = max(int(desired_state.get("pages_committed") or 0), len(chain))
    desired_state["items_returned_total"] = max(
        int(desired_state.get("items_returned_total") or 0),
        sum(int((page.get("statistics") or {}).get("returned") or 0) for page in chain),
    )
    desired_state["last_page_count"] = int((terminal.get("statistics") or {}).get("returned") or 0)
    desired_state["last_committed_page_id"] = str(terminal["page_id"])
    desired_state["request_signature"] = expected_signature
    if desired_state == state:
        return None
    after_hash = _sha256_bytes(_canonical_bytes(desired))
    return {
        **identity,
        "action": "would_restore_current_backfill_state",
        "recoverable": True,
        "source_journals": source_journals,
        "before_sha256": _sha256_file(notebook_path),
        "after_sha256": after_hash,
    }


def recover_notebooks(
    *,
    notebook_dir: Path,
    pending_pages_dir: Path | None = None,
    locks_dir: Path | None = None,
    catalog_root: Path | None = None,
    transaction_root: Path | None = None,
    apply: bool = False,
    tx_id: str | None = None,
) -> dict[str, Any]:
    """Inspect v3 recovery candidates; never write an apply transaction."""
    if apply:
        raise RecoveryApplyUnavailable("v3 recovery is inspect-only; no write entry point is available")
    notebook_root = Path(notebook_dir)
    pages_root = Path(pending_pages_dir) if pending_pages_dir else notebook_root.parent / "pending_pages"
    lock_root = Path(locks_dir) if locks_dir else notebook_root.parent / "locks"
    _ = catalog_root or CATALOG_FOLDER_ROOT
    _ = transaction_root
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    notebooks = _load_notebooks(notebook_root, errors=errors)
    pages = _load_pages(pages_root, errors=errors)
    by_identity: dict[tuple[str, str, str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for page in pages:
        kid = str(page.get("keyword_id") or "")
        qid = str(page.get("query_id") or "")
        provider = str(page.get("provider") or "")
        if page.get("lane") != "backfill":
            continue
        notebook = notebooks.get(kid)
        if notebook is None:
            _issue(errors, "orphan_page", page.get("__path__", ""), "page references an unknown notebook", keyword_id=kid, query_id=qid)
            continue
        query = notebook.get("search_queries", {}).get(qid)
        if query is None:
            _issue(errors, "orphan_page", page.get("__path__", ""), "page references an unknown query", keyword_id=kid, query_id=qid)
            continue
        if query.get("query") != page.get("query") or query.get("language") != page.get("query_language"):
            _issue(errors, "query_identity", page.get("__path__", ""), "page query differs from notebook", keyword_id=kid, query_id=qid)
        try:
            generation = int(page.get("generation") or 0)
        except (TypeError, ValueError):
            generation = 0
        signature = str(page.get("request_signature", {}).get("hash") or "")
        by_identity[(kid, qid, provider, generation, signature)].append(page)

    operations: list[dict[str, Any]] = []
    historical_generations = 0
    current_generations = 0
    for (kid, qid, provider, generation, signature), group in sorted(by_identity.items()):
        notebook = notebooks.get(kid)
        if notebook is None:
            continue
        state = (
            notebook.get("search_queries", {})
            .get(qid, {})
            .get("providers", {})
            .get(provider, {})
            .get("backfill")
        )
        if not isinstance(state, dict):
            _issue(
                errors,
                "query_identity",
                notebook.get("__path__", ""),
                "page query has no notebook backfill state",
                keyword_id=kid,
                query_id=qid,
                provider=provider,
                generation=generation,
                request_signature=signature,
            )
            continue
        current_generation = int(state.get("generation") or 0)
        current_signature = str(state.get("request_signature") or "")
        history = {
            int(row.get("generation") or 0): row
            for row in state.get("generation_history") or []
        }
        if generation == current_generation:
            current_generations += 1
            if signature != current_signature:
                _issue(
                    errors,
                    "signature",
                    group[0].get("__path__", ""),
                    "current generation journal signature differs from notebook state",
                    keyword_id=kid,
                    query_id=qid,
                    provider=provider,
                    generation=generation,
                    request_signature=signature,
                    expected_signature=current_signature,
                )
                continue
            operation = _state_operation(
                notebook,
                group,
                notebook_path=Path(str(notebook["__path__"])),
                base=pages_root,
                errors=errors,
                warnings=warnings,
                generation=generation,
                expected_signature=current_signature,
                current_generation=current_generation,
            )
        elif generation in history:
            historical_generations += 1
            history_signature = str(history[generation].get("request_signature") or "")
            if signature != history_signature:
                _issue(
                    errors,
                    "signature",
                    group[0].get("__path__", ""),
                    "historical generation journal signature differs from history",
                    keyword_id=kid,
                    query_id=qid,
                    provider=provider,
                    generation=generation,
                    request_signature=signature,
                    expected_signature=history_signature,
                )
                continue
            operation = _state_operation(
                notebook,
                group,
                notebook_path=Path(str(notebook["__path__"])),
                base=pages_root,
                errors=errors,
                warnings=warnings,
                generation=generation,
                expected_signature=history_signature,
                current_generation=current_generation,
                history_state=history[generation],
            )
        else:
            _issue(
                errors,
                "generation",
                group[0].get("__path__", ""),
                "journal generation is missing from current state and generation history",
                keyword_id=kid,
                query_id=qid,
                provider=provider,
                generation=generation,
                request_signature=signature,
                current_generation=current_generation,
            )
            continue
        if operation:
            operations.append(operation)

    planned_operations = [] if errors else operations
    plan_body = {
        "schema_version": "1.0",
        "created_at": now_iso(),
        # A report with any identity or chain error is fail-closed.  It may
        # still describe the offending generation in ``errors``, but it must
        # never offer a restore operation alongside unsafe evidence.
        "operations": planned_operations,
    }
    plan_sha256 = _sha256_bytes(_canonical_bytes(plan_body))
    plan = {**plan_body, "plan_sha256": plan_sha256}
    lock_files = sorted(path for path in lock_root.rglob("*") if path.is_file()) if lock_root.is_dir() else []
    return {
        "inspect_only": True,
        "applied": False,
        "transaction_id": tx_id,
        "plan": plan,
        "plan_sha256": plan_sha256,
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "v3_notebooks_scanned": len(notebooks),
            "page_journals_scanned": len(pages),
            "recovery_operations": len(planned_operations),
            "historical_generations": historical_generations,
            "current_generations": current_generations,
            "recoverable_current_generations": sum(
                bool(operation.get("recoverable")) for operation in planned_operations
            ),
            "lock_files_seen": len(lock_files),
            "cursor_divergence": sum(item["kind"] == "cursor_divergence" for item in errors),
            "historical_generation_warnings": sum(
                item["kind"] == "historical_generation" for item in warnings
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect strict v3 discovery recovery candidates.")
    parser.add_argument("--inspect", action="store_true", help="Run the read-only recovery inspection (default).")
    parser.add_argument("--notebook-dir", type=Path, default=DISCOVERY_KEYWORD_NOTEBOOK_DIR)
    parser.add_argument("--pending-pages-dir", type=Path, default=DISCOVERY_PENDING_PAGES_DIR)
    parser.add_argument("--locks-dir", type=Path, default=DISCOVERY_LOCKS_DIR)
    parser.add_argument("--catalog-root", type=Path, default=CATALOG_FOLDER_ROOT)
    parser.add_argument("--transaction-root", type=Path, default=None)
    parser.add_argument("--tx-id", type=str, default=None)
    args = parser.parse_args(argv)
    report = recover_notebooks(
        notebook_dir=args.notebook_dir,
        pending_pages_dir=args.pending_pages_dir,
        locks_dir=args.locks_dir,
        catalog_root=args.catalog_root,
        transaction_root=args.transaction_root,
        tx_id=args.tx_id,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not report["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
