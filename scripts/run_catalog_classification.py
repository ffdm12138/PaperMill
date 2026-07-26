"""Execute pending classification tasks through an injectable backend.

Backends:
  fake         – FakeClassifier (TESTING ONLY, requires --testing-only)
  manual       – ManualResultClassifier: reads pre-written results from a directory
  agent-skill  – exports a batch for external agent processing
  import       – imports results from a directory produced by an external agent

Real classification must be done by an agent (Codex) reading each task's
Catalog and producing real semantic judgments.  The fake backend is
hard-rejected on real data/catalog/ unless --testing-only is passed.

Workflow:
  1. Plan tasks:   python scripts/plan_catalog_classification.py --all --apply
  2. Export batch: python scripts/run_catalog_classification.py --export-batch batch.json
  3. Agent classifies each task (reads Catalog, writes result JSON)
  4. Import:       python scripts/run_catalog_classification.py --import-results dir/ --apply
  5. Doctor:       python scripts/doctor_catalog_folders.py
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from config.settings import CATALOG_FOLDER_ROOT, PAPERS_DIR, PAPER_NUMBER_LEDGER_PATH
from src.catalog_folders.classifier_runner import (
    ManualResultClassifier, export_batch, import_results, run_tasks,
)
from src.catalog_folders.formal_registry import FormalPaperRegistry
from src.library.paper_number_ledger import PaperNumberLedger


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Run pending catalog classification tasks.",
        epilog="Real classification: export batch → agent reads Catalogs → "
               "import results → doctor.  Never use --backend fake on real data.",
    )
    p.add_argument("--catalog-root", type=Path, default=CATALOG_FOLDER_ROOT)
    p.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    p.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    p.add_argument("--backend", choices=["fake", "manual", "agent-skill", "import"],
                   help="Classifier backend. 'fake' requires --testing-only on real data.")
    p.add_argument("--testing-only", action="store_true",
                   help="Acknowledge that --backend fake is for testing only")
    p.add_argument("--result-dir", type=Path,
                   help="Directory of pre-written results (for manual/import backends)")
    p.add_argument("--export-batch", type=Path,
                   help="Export pending tasks to a batch JSON file")
    p.add_argument("--import-results", type=Path,
                   help="Import and apply results from a directory")
    p.add_argument("--max-tasks", type=int, default=None,
                   help="Maximum number of tasks to process")
    p.add_argument("--apply", action="store_true",
                   help="Write assignments, receipts, and reconcile links")
    a = p.parse_args(argv)

    registry = FormalPaperRegistry(
        papers_dir=a.papers_dir,
        ledger=PaperNumberLedger(a.ledger_path),
    )

    # ── export mode ───────────────────────────────────────────────────
    if a.export_batch:
        result = export_batch(
            root=a.catalog_root,
            output_path=a.export_batch,
            max_tasks=a.max_tasks,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["exported"] == 0:
            print("\nNo pending tasks to export. All tasks have been applied.", file=sys.stderr)
        else:
            print(f"\nExported {result['exported']} tasks.", file=sys.stderr)
            print(f"Process each task by reading its Catalog, then run:", file=sys.stderr)
            print(f"  python scripts/run_catalog_classification.py --import-results <dir> --apply",
                  file=sys.stderr)
        return 0

    # ── import mode ───────────────────────────────────────────────────
    result_dir = a.import_results or a.result_dir
    if a.backend in ("import", "manual") or a.import_results:
        if not result_dir:
            p.error("--result-dir is required for manual/import backends")
        if not Path(result_dir).is_dir():
            p.error(f"result directory not found: {result_dir}")
        report = import_results(
            result_dir=result_dir,
            root=a.catalog_root,
            formal_registry=registry,
            apply=a.apply,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if report["errors"]:
            return 1
        return 0

    # ── direct classification (fake/manual) ──────────────────────────
    if not a.backend:
        p.error("choose --backend, --export-batch, or --import-results")

    if a.backend == "fake":
        if not a.testing_only:
            p.error(
                "--backend fake requires --testing-only.  "
                "FakeClassifier must never run on real data/catalog/.  "
                "For real classification, use --export-batch and process tasks "
                "by reading each Catalog individually."
            )
        from src.catalog_folders.testing.fake_classifier import FakeClassifier
        classifier = FakeClassifier()
        classifier_name = "fake"
        testing_only = True
    elif a.backend == "manual":
        if not result_dir:
            p.error("--result-dir is required for manual backend")
        classifier = ManualResultClassifier(result_dir)
        classifier_name = "manual"
        testing_only = False
    else:
        p.error(f"--backend {a.backend} requires --export-batch or --import-results")

    report = run_tasks(
        root=a.catalog_root,
        formal_registry=registry,
        classifier=classifier,
        apply=a.apply,
        max_tasks=a.max_tasks,
        classifier_name=classifier_name,
        testing_only=testing_only,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if report["classified"] > 0:
        print(f"\nClassified {report['classified']} tasks.", file=sys.stderr)
    if report["skipped"] > 0:
        print(f"Skipped {report['skipped']} already-applied tasks.", file=sys.stderr)
    if not a.apply:
        print("Dry run — pass --apply to write assignments and reconcile links.", file=sys.stderr)
    else:
        print("Next: python scripts/doctor_catalog_folders.py", file=sys.stderr)
    return 0 if report["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
