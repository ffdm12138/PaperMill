"""Unsafe legacy rollback all papers from data/papers/ to data/paper_raw/ using paper_number.

For each paper in data/papers/:
  1. Read .paper.number → get 16-digit paper_number
  2. Create data/paper_raw/<paper_number>/
  3. Rename files: <paper_id>.xxx → <paper_number>.xxx
  4. Move images/ dir
  5. Create .import_status.json
  6. Remove data/papers/<paper_id>/
"""
import json, os, shutil, sys, glob

APPLY = '--apply' in sys.argv
DRY_RUN = '--dry-run' in sys.argv or not APPLY
RAW = 'data/paper_raw'
DATA = 'data/papers'
LEDGER_PATH = 'data/catalog/paper_number_ledger.json'

total = 0
errors = []

for d in sorted(os.listdir(DATA)):
    path = os.path.join(DATA, d)
    if not os.path.isdir(path) or d == 'papers':
        continue

    # Read paper_number
    pn_files = glob.glob(os.path.join(path, '*.paper.number'))
    if not pn_files:
        errors.append(f"{d}: no .paper.number file")
        continue
    pn = os.path.basename(pn_files[0]).replace('.paper.number', '')
    raw_path = os.path.join(RAW, pn)

    if DRY_RUN:
        print(f"[DRY RUN] {d} → paper_raw/{pn}/")
        total += 1
        continue

    # Create raw dir
    os.makedirs(raw_path, exist_ok=True)

    # Rename and move files: <paper_id>.xxx → <pn>.xxx
    for fname in os.listdir(path):
        fpath = os.path.join(path, fname)
        if os.path.isfile(fpath):
            if fname.startswith(d + '.'):
                new_name = fname.replace(d, pn, 1)
                shutil.move(fpath, os.path.join(raw_path, new_name))
            elif fname.endswith('.paper.number'):
                # Move .paper.number too, rename to avoid confusion
                shutil.move(fpath, os.path.join(raw_path, f'{pn}.paper.number'))
            else:
                # Unexpected files, move as-is
                shutil.move(fpath, os.path.join(raw_path, fname))
        elif os.path.isdir(fpath) and fname == 'images':
            # Move images/ directory
            shutil.move(fpath, os.path.join(raw_path, 'images'))

    # Create .import_status.json
    import_status = {
        "paper_number": pn,
        "paper_raw_id": pn,
        "status": "converted",  # already converted, just uncommitted
        "converted_at": None,
        "committed_at": None,
        "rolled_back_at": None,
        "note": "rolled back from data/papers"
    }
    with open(os.path.join(raw_path, '.import_status.json'), 'w', encoding='utf-8') as f:
        json.dump(import_status, f, ensure_ascii=False, indent=2)

    # Remove empty paper dir
    remaining = os.listdir(path)
    if not remaining:
        os.rmdir(path)
    else:
        print(f"  WARNING: {d} still has files after move: {remaining}")

    print(f"  {d} → paper_raw/{pn}/")
    total += 1

# Update ledger: remove all items (papers no longer in data/papers)
if APPLY and os.path.exists(LEDGER_PATH):
    with open(LEDGER_PATH, encoding='utf-8') as f:
        ledger = json.load(f)
    ledger['items'] = {}
    ledger['max_number'] = '0000000000000000'
    with open(LEDGER_PATH, 'w', encoding='utf-8') as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)
    print(f"\nLedger cleared ({LEDGER_PATH})")

print(f"\n{'='*60}")
print(f"Mode: {'DRY RUN' if DRY_RUN else 'APPLY'}")
print(f"Total papers processed: {total}")
if errors:
    print(f"Errors: {len(errors)}")
    for e in errors:
        print(f"  {e}")
if DRY_RUN and not errors:
    print("Run with --apply to execute.")
