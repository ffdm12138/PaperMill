#!/usr/bin/env python
"""Manage strict schema-v3 bilingual discovery keyword notebooks.

``keyword_zh`` is the sole Chinese classification identity.  Chinese and
English provider search strings are explicit ``search_queries`` owned by the
notebook.  This command never infers queries or creates Catalog categories.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import DISCOVERY_KEYWORD_NOTEBOOK_DIR  # noqa: E402
from src.discovery.keyword_notebook import (  # noqa: E402
    KeywordNotebookStore,
    NotebookCorruptError,
    detect_query_language,
    pagination_signature,
    validate_discovery_readiness,
)


def _print_keyword_summary(item: dict) -> None:
    print(f"\n{item['keyword_zh']}")
    print(f"  enabled: {'yes' if item['enabled'] else 'no'}")
    print(f"  ready: {'yes' if item['ready'] else 'no'}")
    print(f"  keyword_id: {item['keyword_id']}")
    print(f"  active_queries: {item['active_queries']}")
    for query in item["queries"]:
        inactive = " (inactive)" if not query["active"] else ""
        print(
            f"  query[{query['language']}]: {query['query']!r}"
            f" [{query['query_id']}]{inactive}"
        )
        for provider, state in query["providers"].items():
            print(
                f"    {provider}: refresh={state['refresh_status']} "
                f"backfill_cursor={state['backfill_cursor']} "
                f"exhausted={state['backfill_exhausted']} "
                f"pages={state['backfill_pages']}"
            )


def _query_rows(args: argparse.Namespace) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for language, values in (("zh", args.query_zh), ("en", args.query_en)):
        for query in values:
            detected = detect_query_language(query)
            if detected != language:
                raise ValueError(
                    f"--query-{language} text {query!r} is {detected!r}, not {language!r}"
                )
            rows.append({"query": query, "language": language, "source": args.source})
    return rows


def _readiness_payload(notebook: dict) -> dict:
    result = validate_discovery_readiness(notebook)
    return {
        "keyword_zh": result.keyword_zh,
        "ready": result.ready,
        "active_zh_queries": result.zh_count,
        "active_en_queries": result.en_count,
        "errors": result.errors,
    }


def _require_keyword(value: str | None, parser: argparse.ArgumentParser) -> str:
    if not value:
        parser.error("this operation requires --keyword-zh")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage strict v3 Chinese topics and bilingual search queries.",
    )
    parser.add_argument(
        "--keyword-notebook-dir", type=Path, default=DISCOVERY_KEYWORD_NOTEBOOK_DIR,
    )
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--list", action="store_true", help="List all v3 notebooks.")
    actions.add_argument("--show", action="store_true", help="Show one complete notebook.")
    actions.add_argument("--create", action="store_true", help="Create one Chinese topic.")
    actions.add_argument("--enable", action="store_true", help="Enable one ready topic.")
    actions.add_argument("--disable", action="store_true", help="Disable one topic.")
    actions.add_argument(
        "--add-query-zh", action="store_true", help="Add explicit Chinese queries.",
    )
    actions.add_argument(
        "--add-query-en", action="store_true", help="Add explicit English queries.",
    )
    actions.add_argument(
        "--disable-query", metavar="QUERY", help="Disable one exact query string.",
    )
    actions.add_argument(
        "--enable-query", metavar="QUERY", help="Enable one exact query string.",
    )
    actions.add_argument(
        "--check-ready", action="store_true", help="Validate one or all notebooks.",
    )
    actions.add_argument(
        "--reset-backfill", action="store_true", help="Reset one topic's backfill state.",
    )
    parser.add_argument("--keyword-zh", help="Exact Chinese topic identity.")
    parser.add_argument("--query-zh", action="append", default=[], help="Chinese query; repeatable.")
    parser.add_argument("--query-en", action="append", default=[], help="English query; repeatable.")
    parser.add_argument("--source", default="curated_cli", help="Query provenance label.")
    parser.add_argument("--reason", default="manage_discovery_keywords CLI")
    parser.add_argument("--operator", default="cli")
    parser.add_argument("--sort", default=None, help="Pagination sort for reset signature.")
    parser.add_argument("--apply", action="store_true", help="Apply a planned write operation.")
    parser.add_argument("--create-disabled", action="store_true", help="Create a disabled draft; permits incomplete queries.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = KeywordNotebookStore(args.keyword_notebook_dir)
    try:
        if args.list:
            items = store.list_keywords()
            if not items:
                print("(no keyword notebooks found)")
            for item in items:
                _print_keyword_summary(item)
            return 0

        if args.check_ready and not args.keyword_zh:
            payloads = [
                _readiness_payload(store.require_v3(item["keyword_zh"]))
                for item in store.list_keywords()
            ]
            print(json.dumps(payloads, ensure_ascii=False, indent=2))
            return 0 if all(item["ready"] for item in payloads) else 2

        keyword_zh = _require_keyword(args.keyword_zh, parser)

        if args.show:
            print(json.dumps(store.require_v3(keyword_zh), ensure_ascii=False, indent=2))
            return 0

        if args.create:
            rows = _query_rows(args)
            if not args.create_disabled and {row["language"] for row in rows} != {"zh", "en"}:
                raise ValueError("--create requires at least one --query-zh and --query-en")
            if store.load(keyword_zh) is not None:
                raise ValueError(f"notebook already exists: {keyword_zh!r}")
            if not args.apply:
                print(json.dumps({"would_create": keyword_zh, "enabled": not args.create_disabled,
                                  "queries": rows}, ensure_ascii=False, indent=2))
                return 0
            notebook = store.create_notebook(
                keyword_zh, search_queries=rows, enabled=not args.create_disabled,
                reason=args.reason, operator=args.operator,
            )
            readiness = _readiness_payload(notebook)
            if args.create_disabled:
                print(json.dumps({
                    "status": "created_disabled_draft",
                    "keyword_zh": keyword_zh,
                    "enabled": False,
                    "ready": False,
                    "errors": readiness["errors"],
                }, ensure_ascii=False, indent=2))
                return 0
            if not readiness["ready"]:
                raise RuntimeError(f"new notebook is not ready: {readiness['errors']}")
            print(json.dumps({
                "status": "created",
                "keyword_zh": keyword_zh,
                "enabled": True,
                "ready": True,
                "errors": readiness["errors"],
            }, ensure_ascii=False, indent=2))
            return 0

        if args.add_query_zh or args.add_query_en:
            rows = _query_rows(args)
            selected_language = "zh" if args.add_query_zh else "en"
            if not rows:
                raise ValueError(f"--add-query-{selected_language} requires --query-{selected_language}")
            if any(row["language"] != selected_language for row in rows):
                raise ValueError(
                    f"--add-query-{selected_language} accepts only --query-{selected_language}"
                )
            if not args.apply:
                print(json.dumps({"would_add": rows}, ensure_ascii=False, indent=2))
                return 0
            notebook = store.sync_search_queries(
                keyword_zh, add=rows, reason=args.reason, operator=args.operator,
            )
            print(json.dumps(_readiness_payload(notebook), ensure_ascii=False, indent=2))
            return 0

        if args.disable_query or args.enable_query:
            query = args.disable_query or args.enable_query
            if not args.apply:
                print(json.dumps({"would_disable" if args.disable_query else "would_enable": query}, ensure_ascii=False, indent=2))
                return 0
            notebook = store.sync_search_queries(
                keyword_zh,
                disable=[query] if args.disable_query else None,
                enable=[query] if args.enable_query else None,
                reason=args.reason,
                operator=args.operator,
            )
            print(json.dumps(_readiness_payload(notebook), ensure_ascii=False, indent=2))
            return 0

        if args.enable:
            candidate = deepcopy(store.require_v3(keyword_zh))
            candidate["enabled"] = True
            readiness = validate_discovery_readiness(candidate)
            if not readiness:
                raise ValueError("cannot enable an unready notebook: " + "; ".join(readiness.errors))
            if not args.apply:
                print(json.dumps({"would_enable": keyword_zh}, ensure_ascii=False, indent=2))
                return 0
            store.set_enabled(keyword_zh, True)
            print(f"[OK] enabled: {keyword_zh!r}")
            return 0

        if args.disable:
            if not args.apply:
                print(json.dumps({"would_disable": keyword_zh}, ensure_ascii=False, indent=2))
                return 0
            store.set_enabled(keyword_zh, False)
            print(f"[OK] disabled: {keyword_zh!r}")
            return 0

        if args.check_ready:
            payload = _readiness_payload(store.require_v3(keyword_zh))
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0 if payload["ready"] else 2

        if args.reset_backfill:
            if not args.apply:
                print(json.dumps({"would_reset": keyword_zh}, ensure_ascii=False, indent=2))
                return 0
            store.reset_backfill(
                keyword_zh,
                reason=args.reason,
                pag_sig=pagination_signature(sort=args.sort),
            )
            print(f"[OK] reset backfill for: {keyword_zh!r}")
            print("     paper_raw and existing DOI candidates are not affected")
            return 0
    except (FileNotFoundError, NotebookCorruptError, RuntimeError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
