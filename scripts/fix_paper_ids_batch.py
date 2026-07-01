#!/usr/bin/env python3
"""Batch-rename paper directories and update all cross-references.

Run: /c/Users/Admin/.conda/envs/mineru/python.exe scripts/fix_paper_ids_batch.py
Dry-run first: scripts/fix_paper_ids_batch.py --dry-run
"""

import json
import os
import shutil
import sys
from pathlib import Path

# Fix Windows GBK encoding issues
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

ROOT = Path(__file__).resolve().parent.parent

# (old_paper_id, new_paper_id)
RENAMES = [
    (
        "2005_TANAKA_MASINGAR全球沙尘气溶胶模拟",
        "2005_Tanaka_全球沙尘气溶胶模拟",
    ),
    (
        "2008_DavidsonArnott_海滩湿度对风沙输运阈值与通量影响",
        "2008_Davidson_Arnott_海滩湿度对风沙输运阈值与通量影响",
    ),
    (
        "2012_Vionnet_Crocus_详细雪层方案_SURFEX_实现",
        "2012_Vionnet_Crocus详细雪层方案及SURFEX实现",
    ),
    (
        "2015_Divine_Regional_melt_pond_fraction_and_albedo_of_thin_Arctic_first_year_drift_ice_in_late_summer",
        "2015_Divine_北极一年冰融池反照率",
    ),
    (
        "2024_Gadde_Contribution_of_blowing_snow_sublimation_to_the_surface_mass_balance_of_Antarctica",
        "2024_Gadde_南极吹雪升华物质平衡贡献",
    ),
]

# For English-title papers, also set title.short_zh in metadata
SHORT_ZH_UPDATES = {
    "2015_Divine_北极一年冰融池反照率": "北极一年冰融池反照率",
    "2024_Gadde_南极吹雪升华物质平衡贡献": "南极吹雪升华物质平衡贡献",
}

# Extra files to delete (relative to paper dir)
EXTRA_FILES_TO_DELETE = [
    "2014_Vionnet_高山风吹雪耦合模拟/duplicate_report.json",
]


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  ✓ updated {path.relative_to(ROOT)}")


def str_replace_in_file(path, old, new):
    """Replace all occurrences of old with new in a text file."""
    content = path.read_text(encoding="utf-8")
    if old not in content:
        print(f"  ⚠ WARNING: '{old}' not found in {path.relative_to(ROOT)}")
        return False
    content = content.replace(old, new)
    path.write_text(content, encoding="utf-8")
    print(f"  ✓ replaced in {path.relative_to(ROOT)}")
    return True


def dry_run():
    print("=== DRY RUN ===\n")
    for old_id, new_id in RENAMES:
        old_dir = ROOT / "data" / "papers" / old_id
        new_dir = ROOT / "data" / "papers" / new_id
        exists = old_dir.exists()
        print(f"  {'✓' if exists else '✗'} {old_id}")
        print(f"    → {new_id}")
        if exists:
            for f in old_dir.iterdir():
                if f.is_file():
                    old_name = f.name
                    new_name = old_name.replace(old_id, new_id)
                    if old_name != new_name:
                        print(f"      rename: {old_name} → {new_name}")
    for rel_path in EXTRA_FILES_TO_DELETE:
        p = ROOT / "data" / "papers" / rel_path
        print(f"  delete: {rel_path}  {'(exists)' if p.exists() else '(missing)'}")
    print("\nRun without --dry-run to apply.")


def apply():
    print("=== Applying fixes ===\n")

    # --- Step 1: Rename directories and files ---
    print("[1/6] Renaming paper directories and internal files...")
    for old_id, new_id in RENAMES:
        old_dir = ROOT / "data" / "papers" / old_id
        new_dir = ROOT / "data" / "papers" / new_id

        if not old_dir.exists():
            print(f"  ✗ directory not found: {old_dir}")
            continue

        # Rename files inside the directory first
        for f in sorted(old_dir.iterdir()):
            if f.is_file():
                old_name = f.name
                new_name = old_name.replace(old_id, new_id)
                if old_name != new_name:
                    f.rename(f.parent / new_name)
                    print(f"  renamed file: {old_name} → {new_name}")
            elif f.is_dir():
                pass  # images/ directory stays

        # Rename the directory itself
        old_dir.rename(new_dir)
        print(f"  renamed dir: {old_id} → {new_id}")

    # --- Step 2: Delete extra files ---
    print("\n[2/6] Deleting extra files...")
    for rel_path in EXTRA_FILES_TO_DELETE:
        p = ROOT / "data" / "papers" / rel_path
        if p.exists():
            p.unlink()
            print(f"  ✓ deleted {rel_path}")
        else:
            print(f"  - already missing: {rel_path}")

    # --- Step 3: Update metadata.json (pdf.path + short_zh) ---
    print("\n[3/6] Updating metadata.json files...")
    for old_id, new_id in RENAMES:
        meta_path = ROOT / "data" / "papers" / new_id / f"{new_id}.metadata.json"
        if not meta_path.exists():
            print(f"  ✗ metadata not found: {meta_path}")
            continue
        data = load_json(meta_path)
        # Update pdf.path
        if "pdf" in data and "path" in data["pdf"]:
            old_path = data["pdf"]["path"]
            data["pdf"]["path"] = old_path.replace(old_id, new_id)
        # Set short_zh for English-title papers
        if new_id in SHORT_ZH_UPDATES:
            data.setdefault("title", {})
            data["title"]["short_zh"] = SHORT_ZH_UPDATES[new_id]
        save_json(meta_path, data)

    # --- Step 4: Update catalog.json ---
    print("\n[4/6] Updating catalog.json files...")
    for old_id, new_id in RENAMES:
        cat_path = ROOT / "data" / "papers" / new_id / f"{new_id}.catalog.json"
        if not cat_path.exists():
            print(f"  ✗ catalog not found: {cat_path}")
            continue
        str_replace_in_file(cat_path, old_id, new_id)

    # --- Step 5: Update paper_index.json ---
    print("\n[5/6] Updating paper_index.json...")
    idx_path = ROOT / "data" / "catalog" / "paper_index.json"
    idx_data = load_json(idx_path)
    for entry in idx_data["papers"]:
        old_id = entry["paper_id"]
        for o, n in RENAMES:
            if old_id == o:
                entry["paper_id"] = n
                for key in list(entry.keys()):
                    if isinstance(entry[key], str) and o in entry[key]:
                        entry[key] = entry[key].replace(o, n)
                break
    save_json(idx_path, idx_data)

    # --- Step 6: Update paper_number_ledger.json + all.catalog.json ---
    print("\n[6/6] Updating ledger and all.catalog...")
    ledger_path = ROOT / "data" / "catalog" / "paper_number_ledger.json"
    str_replace_in_file(ledger_path, "", "")  # no-op, just verify readable
    ledger = load_json(ledger_path)
    for number, item in ledger["items"].items():
        for o, n in RENAMES:
            if item.get("folder_name") == o:
                item["folder_name"] = n
                item["folder_path"] = f"data/papers/{n}"
                break
    save_json(ledger_path, ledger)

    all_cat_path = ROOT / "data" / "catalog" / "all.catalog.json"
    for o, n in RENAMES:
        str_replace_in_file(all_cat_path, o, n)

    print("\n=== Done ===")
    print("Run validation: python scripts/validate_v2_library.py")
    print("Run rebuild:    python scripts/rebuild_all_catalog.py --apply")


if __name__ == "__main__":
    if "--dry-run" in sys.argv:
        dry_run()
    else:
        apply()
