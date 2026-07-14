"""FakeClassifier — test-only, deterministic matched=True for every category.

DO NOT import this module in production code paths.  It exists only for
automated tests and is gated by ``--testing-only`` in the CLI.
"""
from __future__ import annotations


class FakeClassifier:
    """Always returns matched=True with medium confidence for every category.

    This is NOT a real classifier — it exists only for automated testing
    of the classification pipeline (task planning, result application,
    link reconciliation).
    """

    def classify(self, *, task: dict, catalog: dict[str, object]) -> dict:
        decisions = []
        for cat in task["categories"]:
            decisions.append({
                "category_id": cat["category_id"],
                "matched": True,
                "confidence": "medium",
                "reason_zh": f"fake classification for {cat['keyword_zh']}",
                "catalog_evidence_fields": list(catalog.keys())[:3],
            })
        return {
            "schema_version": "1.0",
            "task_id": task["task_id"],
            "task_input_sha256": task["task_input_sha256"],
            "paper_number": task["paper_number"],
            "paper_name": task["paper_name"],
            "decisions": decisions,
        }
