from __future__ import annotations

from pathlib import Path

from src.catalog_folders.assignment import load_assignment, valid_decisions
from src.catalog_folders.formal_registry import FormalPaperRegistry
from src.catalog_folders.reader import list_categories, read_category_members
from src.catalog_folders.registry import load_categories
from src.discovery.keyword_notebook import (
    validate_discovery_readiness,
    validate_notebook,
)


def doctor(*, root: Path, formal_registry: FormalPaperRegistry,
           notebook_dir=None, transaction_root=None,
           allow_empty_categories: bool = False) -> dict:
    """Diagnose catalog folder state.

    Returns a structured report with split safety flags:
      - folder_integrity_safe: link structure is not corrupted
      - classification_complete: every formal paper has a valid decision
        for every active category
      - writer_category_safe: writer can safely rely on keyword categories
        without silently missing papers
    """
    root = Path(root); errors: list[str] = []
    # Per-category error lists for structured reporting
    unmanaged_members: list[str] = []
    identity_mismatch_links: list[str] = []
    old_number_named_links: list[str] = []
    notebook_parse_errors: list[str] = []
    notebook_schema_errors: list[str] = []
    discovery_query_errors: list[str] = []
    keyword_collisions: list[str] = []
    unfinished_apply_journals: list[str] = []
    unfinished_migration_journals: list[str] = []
    try:
        papers = formal_registry.load(refresh=True)
    except Exception as exc:
        return {
            "folder_integrity_safe": False,
            "classification_complete": False,
            "writer_category_safe": False,
            "notebook_schema_safe": False,
            "discovery_query_ready": False,
            "discovery_query_errors": [],
            "errors": [str(exc)],
            "dirty": (root / ".state" / "DIRTY").exists(),
        }
    formal_count = len(papers)
    expected = {paper.paper_number for paper in papers}

    # --- all membership ---
    all_numbers: set[str] = set()
    try:
        all_numbers = {row["paper_number"] for row in read_category_members(root / "all", papers_dir=formal_registry.papers_dir)}
    except Exception as exc:
        errors.append(str(exc))
    if all_numbers != expected:
        errors.append(f"all membership mismatch: missing={sorted(expected-all_numbers)} extra={sorted(all_numbers-expected)}")

    # --- strict Registry + category folders ---
    categories = []
    try:
        registry_path = root / ".state" / "category_registry.json"
        categories = load_categories(registry_path) if registry_path.is_file() else []
    except Exception as exc:
        errors.append(f"category registry invalid: {exc}")
    category_count = len(categories)
    for category_dir in list_categories(root):
        try:
            read_category_members(category_dir, papers_dir=formal_registry.papers_dir)
        except Exception as exc:
            errors.append(str(exc))

    # --- empty-category fail-closed ---
    if formal_count > 0 and category_count == 0 and not allow_empty_categories:
        errors.append(
            "classification configuration invalid: "
            f"{formal_count} formal papers but 0 active categories; "
            "run sync_catalog_categories.py or pass --allow-empty-categories"
        )

    # --- DIRTY ---
    dirty = (root / ".state" / "DIRTY").exists()
    if dirty:
        errors.append("catalog folder state is DIRTY")

    # --- pending / classification completeness ---
    pending_count = 0
    missing_decision_count = 0
    stale_decision_count = 0
    try:
        pending_members = read_category_members(root / "_pending", papers_dir=formal_registry.papers_dir)
        pending_count = len(pending_members)
    except Exception as exc:
        errors.append(str(exc))

    if category_count > 0:
        for paper in papers:
            assignment = load_assignment(root / ".state" / "assignments" / f"{paper.paper_number}.json")
            decisions = valid_decisions(assignment, paper, categories)
            if len(decisions) != len(categories):
                missing_decision_count += 1
            else:
                # check for stale decisions (definition hash or skill version mismatch
                # already filtered by valid_decisions, so stale means the set is incomplete)
                pass
        stale = {
            paper.paper_number
            for paper in papers
            if load_assignment(root / ".state" / "assignments" / f"{paper.paper_number}.json") is not None
            and len(valid_decisions(
                load_assignment(root / ".state" / "assignments" / f"{paper.paper_number}.json"),
                paper, categories,
            )) < len(categories)
        }
        stale_decision_count = len(stale)

    # --- task / result counts ---
    task_count = 0
    unapplied_result_count = 0
    tasks_dir = root / ".state" / "tasks"
    if tasks_dir.is_dir():
        for task_file in tasks_dir.rglob("*.json"):
            task_count += 1
    results_dir = root / ".state" / "applied_results"
    if results_dir.is_dir():
        applied = {p.name for p in results_dir.rglob("*.json") if p.is_file()}
        # unapplied = tasks without matching applied receipt
        if tasks_dir.is_dir():
            for task_file in tasks_dir.rglob("*.json"):
                task_id = task_file.stem
                paper_number = task_file.parent.name
                if not (results_dir / paper_number / f"{task_id}.json").exists():
                    unapplied_result_count += 1

    # --- broken / escaping links ---
    broken_link_count = 0
    escaping_link_count = 0
    unknown_dir_count = 0
    papers_root = formal_registry.papers_dir.resolve()
    import re as _re_scan
    _PAPER_NUMBER_RE_SCAN = _re_scan.compile(r"^\d{16}$")
    from src.catalog_folders.link_backend import inspect_paper_link as _inspect
    for cat_dir in [root / "all", root / "_pending", *list_categories(root)]:
        if not cat_dir.is_dir():
            continue
        for child in cat_dir.iterdir():
            if child.name == ".category.json":
                continue
            link = _inspect(child)
            if link is None:
                unknown_dir_count += 1
                unmanaged_members.append(str(child))
            else:
                if child.name != link.target.name:
                    identity_mismatch_links.append(str(child))
                if _PAPER_NUMBER_RE_SCAN.match(child.name):
                    old_number_named_links.append(str(child))
                if papers_root not in link.target.parents:
                    escaping_link_count += 1

    # --- category directory name hygiene ---
    import re as _re
    _HAS_CJK = _re.compile(r"[一-鿿㐀-䶿豈-﫿]")
    _SUFFIXED = _re.compile(r".+__[0-9a-f]{8}$")
    english_category_dirs: list[str] = []
    suffixed_legacy_dirs: list[str] = []
    missing_category_dirs: list[str] = []
    unknown_category_dirs: list[str] = []
    notebook_registry_drift: list[str] = []

    active_notebook_keywords: set[str] = set()
    disabled_notebook_keywords: set[str] = set()
    import json as _json
    # Only validate notebooks when notebook_dir is explicitly provided.
    # When None, skip notebook-based checks entirely to avoid contaminating
    # isolated test catalogs with real production notebook data.
    if notebook_dir is not None:
        nb_dir = Path(notebook_dir) if Path(notebook_dir).is_dir() else None
    else:
        nb_dir = None
    if nb_dir:
        seen_keywords: dict[str, str] = {}  # keyword -> notebook filename
        for nb in sorted(nb_dir.glob("*.json")):
            try:
                raw = nb.read_text(encoding="utf-8")
                data = _json.loads(raw)
            except _json.JSONDecodeError as exc:
                notebook_parse_errors.append(f"{nb.name}: {exc}")
                continue
            except Exception as exc:
                notebook_parse_errors.append(f"{nb.name}: {exc}")
                continue
            try:
                data = validate_notebook(data)
            except Exception as exc:
                notebook_schema_errors.append(f"{nb.name}: {exc}")
                continue
            kw = data["keyword_zh"]
            if not data["enabled"]:
                disabled_notebook_keywords.add(kw)
                continue
            readiness = validate_discovery_readiness(data)
            if not readiness.ready:
                discovery_query_errors.extend(
                    f"{nb.name}: {message}" for message in readiness.errors
                )
            # Keyword collision detection (only among active notebooks)
            if kw in seen_keywords:
                keyword_collisions.append(f"{kw}: {seen_keywords[kw]} vs {nb.name}")
            else:
                seen_keywords[kw] = nb.name
            active_notebook_keywords.add(kw)

    for cat_dir in list_categories(root):
        dn = cat_dir.name
        # Check for English (no CJK)
        if not _HAS_CJK.search(dn):
            english_category_dirs.append(dn)
        # Check for suffixed legacy
        if _SUFFIXED.match(dn):
            suffixed_legacy_dirs.append(dn)

    # Check for missing dirs (active notebook keyword without matching directory)
    if nb_dir is not None:
        for kw in sorted(active_notebook_keywords):
            if not (root / kw).is_dir():
                missing_category_dirs.append(kw)

    # Check for unknown dirs (dir exists but not in active notebooks)
    if nb_dir is not None:
        for cat_dir in list_categories(root):
            if cat_dir.name not in active_notebook_keywords:
                unknown_category_dirs.append(cat_dir.name)

    # Check notebook/registry drift (only when notebook_dir was provided)
    if category_count > 0 and nb_dir is not None:
        reg_kws = {c.keyword_zh for c in categories if c.keyword_zh}
        for kw in sorted(active_notebook_keywords - reg_kws):
            notebook_registry_drift.append(f"notebook keyword not in registry: {kw}")
        for kw in sorted(reg_kws - active_notebook_keywords):
            notebook_registry_drift.append(f"registry keyword not in notebooks: {kw}")

    if english_category_dirs:
        errors.append(f"English category directories (no CJK): {', '.join(english_category_dirs)}")
    if suffixed_legacy_dirs:
        errors.append(f"Suffixed legacy directories (use migration script): {', '.join(suffixed_legacy_dirs)}")
    if missing_category_dirs:
        errors.append(f"Notebook keywords missing category dirs: {', '.join(missing_category_dirs)}")
    if unknown_category_dirs:
        errors.append(f"Category dirs without matching notebook keyword: {', '.join(unknown_category_dirs)}")
    if notebook_registry_drift:
        errors.extend(notebook_registry_drift)

    # --- notebook validation errors → errors list ---
    if notebook_parse_errors:
        errors.append(f"Notebook parse errors ({len(notebook_parse_errors)}): {', '.join(notebook_parse_errors[:5])}")
    if notebook_schema_errors:
        errors.append(f"Notebook schema errors ({len(notebook_schema_errors)}): {', '.join(notebook_schema_errors[:5])}")
    if keyword_collisions:
        errors.append(f"Keyword collisions ({len(keyword_collisions)}): {', '.join(keyword_collisions[:5])}")
    if unmanaged_members:
        errors.append(f"Unmanaged category members ({len(unmanaged_members)}): {', '.join(unmanaged_members[:5])}")
    if identity_mismatch_links:
        errors.append(f"Identity mismatch links ({len(identity_mismatch_links)}): {', '.join(identity_mismatch_links[:5])}")
    if old_number_named_links:
        errors.append(f"Old number-named links ({len(old_number_named_links)}): {', '.join(old_number_named_links[:5])}")

    # --- transaction journal checks ---
    # Apply journals always live under catalog state
    apply_journal_dir = root / ".state" / "apply_journal"
    if apply_journal_dir.is_dir():
        for journal_file in sorted(apply_journal_dir.rglob("*.json")):
            try:
                jdata = _json.loads(journal_file.read_text(encoding="utf-8"))
                jstate = jdata.get("state", "")
                if jstate not in ("committed", "rolled_back"):
                    unfinished_apply_journals.append(str(journal_file))
            except Exception:
                unfinished_apply_journals.append(
                    f"{journal_file}: unreadable (corrupt journal)")
    if unfinished_apply_journals:
        errors.append(f"Unfinished apply journals ({len(unfinished_apply_journals)}): {', '.join(unfinished_apply_journals[:5])}")

    # Migration and recovery journals live under transaction_root if provided.
    # Discovery migration transactions contain JSON backups below each
    # transaction directory; those payloads are not journals and may carry a
    # domain ``state`` such as ``fetched``.  Inspect only the durable journal
    # paths so backup contents cannot make a committed migration look open.
    if transaction_root:
        tx_path = Path(transaction_root)
        journal_paths = [
            (tx_path / "catalog_keyword_index").glob("*.json"),
            (tx_path / "discovery_keyword_v3").glob("*/journal.json"),
        ]
        for journal_group in journal_paths:
            for journal_file in sorted(journal_group):
                if not journal_file.is_file():
                    continue
                try:
                    jdata = _json.loads(journal_file.read_text(encoding="utf-8"))
                    jstate = jdata.get("state", "")
                    if jstate not in ("committed", "rolled_back", ""):
                        unfinished_migration_journals.append(str(journal_file))
                except Exception:
                    unfinished_migration_journals.append(
                        f"{journal_file}: unreadable (corrupt journal)")
    if unfinished_migration_journals:
        errors.append(f"Unfinished migration journals ({len(unfinished_migration_journals)}): {', '.join(unfinished_migration_journals[:5])}")

    # --- safety flags ---
    folder_integrity_safe = (
        all_numbers == expected
        and broken_link_count == 0
        and escaping_link_count == 0
        and unknown_dir_count == 0
        and not dirty
    )
    classification_complete = (
        formal_count > 0
        and category_count > 0
        and pending_count == 0
        and missing_decision_count == 0
        and stale_decision_count == 0
        and unapplied_result_count == 0
        and len(unfinished_apply_journals) == 0
    ) or (formal_count == 0)
    writer_category_safe = (
        folder_integrity_safe
        and classification_complete
        and pending_count == 0
    )

    # ── fail-closed: any errors → all safety flags false ──────────
    if errors:
        folder_integrity_safe = False
        classification_complete = False
        writer_category_safe = False

    return {
        "active_formal_papers": formal_count,
        "all_members": len(all_numbers),
        "pending": pending_count,
        "categories": category_count,
        "missing_decisions": missing_decision_count,
        "stale_decisions": stale_decision_count,
        "classification_tasks": task_count,
        "unapplied_results": unapplied_result_count,
        "broken_links": broken_link_count,
        "escaping_links": escaping_link_count,
        "unknown_directories": unknown_dir_count,
        "english_category_dirs": english_category_dirs,
        "suffixed_legacy_dirs": suffixed_legacy_dirs,
        "missing_category_dirs": missing_category_dirs,
        "unknown_category_dirs": unknown_category_dirs,
        "notebook_registry_drift": notebook_registry_drift,
        "active_notebook_keywords": sorted(active_notebook_keywords) if active_notebook_keywords else [],
        "dirty": dirty,
        "folder_integrity_safe": folder_integrity_safe,
        "classification_complete": classification_complete,
        "writer_category_safe": writer_category_safe,
        "notebook_schema_safe": not notebook_parse_errors and not notebook_schema_errors,
        "discovery_query_ready": not discovery_query_errors,
        "discovery_query_errors": discovery_query_errors,
        "errors": errors,
        "unmanaged_members": unmanaged_members,
        "identity_mismatch_links": identity_mismatch_links,
        "old_number_named_links": old_number_named_links,
        "notebook_parse_errors": notebook_parse_errors,
        "notebook_schema_errors": notebook_schema_errors,
        "keyword_collisions": keyword_collisions,
        "unfinished_apply_journals": unfinished_apply_journals,
        "unfinished_migration_journals": unfinished_migration_journals,
    }
