"""Search scholarly APIs and write DOI candidates for manual review."""
import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (  # noqa: E402
    DISCOVERY_DIR,
    PAPER_NUMBER_LEDGER_PATH,
    PAPER_RAW_DIR,
    PAPERS_DIR,
)
from src.discovery.models import normalize_doi  # noqa: E402
from src.discovery.pipeline import discover_papers  # noqa: E402
from src.services.metadata_quality import is_valid_normalized_doi  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover DOI candidates from OpenAlex and CrossRef.")
    parser.add_argument("query", help="Chinese or English literature search query.")
    parser.add_argument("--topic", default=None)
    parser.add_argument("--limit-per-query", type=int, default=15)
    parser.add_argument("--max-candidates", type=int, default=50)
    parser.add_argument("--output-dir", type=Path, default=DISCOVERY_DIR / "doi_candidates")
    # direct staging into paper_raw
    parser.add_argument("--stage-to-paper-raw", action="store_true",
                        help="Stage valid-DOI candidates directly into paper_raw workspaces.")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--skip-duplicates", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Allocate paper_raw workspaces (without this, dry-run only).")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--hide-existing", action="store_true",
                        help="Hide candidates whose DOI already exists in paper_raw or papers from discovery JSONL.")
    args = parser.parse_args()

    batch = discover_papers(
        args.query,
        domain_id=args.topic,
        limit_per_query=args.limit_per_query,
        max_candidates=args.max_candidates,
        output_dir=args.output_dir,
        paper_raw_dir=args.paper_raw_dir,
        papers_dir=args.papers_dir,
        hide_existing=args.hide_existing,
    )
    print(f"[OK] candidates: {len(batch.candidates)}")
    for candidate in batch.candidates[:10]:
        doi = candidate.doi or "no DOI"
        print(f"- {candidate.confidence:.2f} | {candidate.year or ''} | {doi} | {candidate.title}")
    print(f"[OK] wrote JSONL + summary under: {args.output_dir}")

    if args.stage_to_paper_raw:
        from src.services.network_metadata_staging import stage_network_metadata_records

        records = []
        for candidate in batch.candidates:
            nd = normalize_doi(candidate.doi)
            if not nd or not is_valid_normalized_doi(nd):
                continue  # network search metadata requires a valid DOI; no LLM DOI fill
            records.append(candidate.to_dict())
        print(f"[STAGE] {len(records)} valid-DOI candidates to stage (apply={args.apply}, dry_run={args.dry_run})")

        report = stage_network_metadata_records(
            records,
            paper_raw_dir=args.paper_raw_dir,
            papers_dir=args.papers_dir,
            ledger_path=args.ledger_path,
            apply=args.apply,
            dry_run=args.dry_run,
            skip_duplicates=args.skip_duplicates,
        )
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[STAGE] staged={report['staged']} duplicate={report['duplicate']} failed={report['failed']} planned={report['planned']}")
        return report["exit_code"]

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
