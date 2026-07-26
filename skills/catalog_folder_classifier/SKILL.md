---
name: catalog-folder-classifier
description: Classify one formal paper into DOI-notebook Chinese keyword folders by reading only its independent Catalog task.
---

# Catalog folder classifier

## Workflow

1. Receive a task from `data/catalog/.state/tasks/<paper_number>/<task_id>.json`.
2. Read only the Catalog at the task's `catalog_path`.
3. For every category in the task, decide independently whether the paper
   substantially belongs based on the Catalog's research question, methods,
   objects, findings, and scope.
4. Write ONE result file per task to the results path.
5. Run the apply script to validate and merge:

```bash
python scripts/apply_catalog_classification_result.py --result <path> --apply
```

Or use the batch runner:

```bash
python scripts/run_catalog_classification.py --backend agent-skill --max-tasks 20

# Or export for external processing:
python scripts/run_catalog_classification.py --export-batch data/catalog/.state/export/classification_batch.json

# After processing, import results:
python scripts/run_catalog_classification.py --import-results <result-directory> --apply

# Verify:
python scripts/reconcile_catalog_folders.py --apply
python scripts/doctor_catalog_folders.py
```

## Rules

Do not use title substring matching, discovery provenance, `research_domains`
as an automatic truth, other papers, full text, PDF, network search, or any
retired global index. Multiple matches and zero matches are both valid.

Catalog folders are sourced only from enabled schema-v4 notebooks. The Chinese
`keyword_zh` is the category identity; English `search_queries` are discovery
inputs and never become categories or directory names.

Write only the result JSON. Do not modify links, assignments, registries,
the ledger, or formal paper assets. Follow `category_result_schema.json`
exactly; cite only top-level fields that actually exist in the current Catalog.

## Commands

```bash
# Plan tasks for all papers
python scripts/plan_catalog_classification.py --all --apply

# Run with fake backend (testing)
python scripts/run_catalog_classification.py --backend fake --apply

# Export batch for external agent processing
python scripts/run_catalog_classification.py --export-batch data/catalog/.state/export/classification_batch.json

# Import results from external processing
python scripts/run_catalog_classification.py --import-results <result-directory> --apply

# Reconcile links
python scripts/reconcile_catalog_folders.py --apply

# Check state
python scripts/doctor_catalog_folders.py
```
