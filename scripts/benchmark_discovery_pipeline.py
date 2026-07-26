#!/usr/bin/env python
"""Small synthetic raw+formal discovery pipeline I/O benchmark."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.discovery.runtime.batch_runtime import ActiveRelevanceProfiles, DiscoveryBatchRuntime
from src.discovery.formal_publication import publish_formal_publication_state
from src.discovery.contracts.notebook import keyword_id, query_identity
from src.discovery.models import PaperCandidate
from src.discovery.contracts.page_journal import request_signature
from src.discovery.stores.page_journal_store import PageJournalStoreV4 as PageJournalStore
from src.discovery.pending_queue import drain_pending_candidates
from src.library.paper_number_ledger import PaperNumberLedger
from tests.factories.paper_raw_factory import (
    create_network_metadata_workspaces_bulk,
    write_minimal_formal_publication_identity,
)


def run_benchmark(*, raw_workspaces: int, formal_workspaces: int,
                  pending_candidates: int, batch_size: int, repeat: int) -> dict:
    if batch_size != 16:
        raise ValueError("the active staging transaction batch size is fixed at 16")
    runs = []
    for run_number in range(1, repeat + 1):
        with tempfile.TemporaryDirectory(prefix="mineru-discovery-pipeline-") as temp:
            root = Path(temp)
            total = raw_workspaces + formal_workspaces
            repair_backlog = min(40, max(0, total))
            create_network_metadata_workspaces_bulk(
                root, count=total, unsettled=repair_backlog)
            ledger = PaperNumberLedger(root / "ledger.json")
            data = ledger.load()
            for index in range(raw_workspaces + 1, total + 1):
                number = f"{index:016d}"
                raw = root / "paper_raw" / number
                formal = root / "papers" / f"formal-{number}"
                formal.parent.mkdir(parents=True, exist_ok=True)
                raw.rename(formal)
                data["items"][number].update(
                    state="active", folder_name=formal.name, folder_path=str(formal),
                    paper_name=formal.name)
                write_minimal_formal_publication_identity(
                    formal, paper_number=number, paper_name=formal.name)
            ledger.save(data)
            publish_formal_publication_state(
                papers_dir=root / "papers", ledger_items=data["items"],
                allow_initialize=True,
            )

            new_count = pending_candidates * 3 // 5
            formal_hits = pending_candidates // 5
            raw_hits = pending_candidates // 10
            repeated = pending_candidates - new_count - formal_hits - raw_hits
            candidates = (
                [PaperCandidate(title=f"New {index}", year=2026,
                                doi=f"10.9400/candidate.{index}")
                 for index in range(new_count)]
                + [PaperCandidate(title=f"Formal {index}", year=2026,
                                  doi=f"10.7000/bench.{raw_workspaces + 1 + (index % max(1, formal_workspaces))}")
                   for index in range(formal_hits)]
                + [PaperCandidate(title=f"Raw {index}", year=2026,
                                  doi=f"10.7000/bench.{1 + (index % max(1, raw_workspaces))}")
                   for index in range(raw_hits)]
                + [PaperCandidate(title=f"Repeat {index}", year=2026,
                                  doi=f"10.9400/candidate.{index % max(1, new_count)}")
                   for index in range(repeated)]
            )
            journal = PageJournalStore(root / "pages")
            kid = keyword_id("性能基准")
            profile_hash = "benchmark-active-profile"
            page_count = min(40, max(1, pending_candidates))
            base_page_size, extra = divmod(pending_candidates, page_count)
            offset = 0
            for page_number in range(page_count):
                page_size = base_page_size + (1 if page_number < extra else 0)
                chunk = candidates[offset:offset + page_size]
                offset += page_size
                page = journal.make_synthetic_page(
                    page_id=f"benchmark-{page_number}", keyword_id=kid,
                    keyword_zh="性能基准", query_id=query_identity("zh", "性能基准"),
                    query="性能基准", query_language="zh", provider="crossref",
                    lane="refresh", request_signature_value=request_signature(page_size=len(chunk)),
                    request_cursor=None, next_cursor=None,
                    provider_exhausted=offset >= pending_candidates,
                    candidates=chunk, state="cursor_committed",
                    relevance_profile_hash=profile_hash,
                )
                for candidate in page["candidates"]:
                    candidate["relevance"]["state"] = "passed"
                journal.write_page(page)
            started = time.perf_counter()
            runtime = DiscoveryBatchRuntime.create(
                journal=journal, paper_raw_dir=root / "paper_raw",
                papers_dir=root / "papers", ledger_path=root / "ledger.json",
                needs_staging=True, persist_repair_cursor=True,
                active_relevance_profiles=ActiveRelevanceProfiles.build(
                    {kid: profile_hash}))
            report = drain_pending_candidates(
                journal=journal, keyword_ids=[kid], candidate_budget=pending_candidates,
                stage_to_paper_raw=True, apply=True,
                paper_raw_dir=root / "paper_raw", papers_dir=root / "papers",
                ledger_path=root / "ledger.json", locks_dir=root / "locks",
                exports_dir=root / "exports", worker_id="benchmark", runtime=runtime)
            run_metrics = runtime.metrics.to_dict()
            runs.append({**run_metrics,
                "run": run_number, "seconds": round(time.perf_counter() - started, 3),
                "allocated": report.staged, "existing_duplicate": report.existing_duplicate,
                "duplicate_observation": report.duplicate_observation,
                "repair_backlog": repair_backlog,
                "journal_page_count": page_count,
            })
    return {"config": {"raw_workspaces": raw_workspaces,
                        "formal_workspaces": formal_workspaces,
                        "pending_candidates": pending_candidates,
                        "batch_size": batch_size, "repeat": repeat}, "runs": runs}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-workspaces", type=int, required=True)
    parser.add_argument("--formal-workspaces", type=int, required=True)
    parser.add_argument("--pending-candidates", type=int, required=True)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--json-report", type=Path, required=True)
    args = parser.parse_args(argv)
    report = run_benchmark(raw_workspaces=args.raw_workspaces,
                           formal_workspaces=args.formal_workspaces,
                           pending_candidates=args.pending_candidates,
                           batch_size=args.batch_size, repeat=args.repeat)
    args.json_report.parent.mkdir(parents=True, exist_ok=True)
    args.json_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
