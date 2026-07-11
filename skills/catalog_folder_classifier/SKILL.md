---
name: catalog-folder-classifier
description: Classify one formal paper into DOI-notebook Chinese keyword folders by reading only its independent Catalog task.
---

# Catalog folder classifier

Read the task's `catalog_path` only. For every supplied category, decide independently whether the paper substantially belongs to that category based on the Catalog's research question, methods, objects, findings, and scope.

Do not use title substring matching, discovery provenance, `research_domains` as an automatic truth, other papers, full text, PDF, network search, or any retired global index. Multiple matches and zero matches are both valid.

Write only the result JSON at the requested results path. Do not modify links, assignments, registries, the ledger, or formal paper assets. Follow `category_result_schema.json` exactly; cite only top-level fields that actually exist in the current Catalog.
