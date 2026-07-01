import sys, os, json
sys.path.insert(0, 'e:/1/mineru')
os.chdir('e:/1/mineru')

from src.services.v2_library import paper_id_from_metadata_catalog

RAW = 'data/paper_raw'
fixed = 0
errors = []

for d in sorted(os.listdir(RAW)):
    path = os.path.join(RAW, d)
    if not os.path.isdir(path) or d in ('papers', 'quarantine'):
        continue

    meta_path = os.path.join(path, f'{d}.metadata.json')
    cat_path = os.path.join(path, f'{d}.catalog.json')
    if not (os.path.exists(meta_path) and os.path.exists(cat_path)):
        continue

    with open(meta_path, encoding='utf-8') as f:
        meta = json.load(f)
    try:
        with open(cat_path, encoding='utf-8') as f:
            cat = json.load(f)
    except Exception as e:
        errors.append((d, f"cat JSON: {e}"))
        continue

    try:
        expected = paper_id_from_metadata_catalog(meta, cat)
    except Exception as e:
        errors.append((d, str(e)))
        continue

    if expected == d:
        continue

    new_path = os.path.join(RAW, expected)
    if os.path.exists(new_path):
        errors.append((d, f"target {expected} exists"))
        continue

    for fname in list(os.listdir(path)):
        fpath = os.path.join(path, fname)
        if fname == 'images' or fname.startswith('.'):
            continue
        if os.path.isfile(fpath):
            if fname.startswith(d + '.'):
                os.rename(fpath, os.path.join(path, fname.replace(d, expected, 1)))
            elif fname.endswith('.paper.number'):
                os.rename(fpath, os.path.join(path, f'{expected}.paper.number'))

    os.rename(path, new_path)

    new_cat = os.path.join(new_path, f'{expected}.catalog.json')
    if os.path.exists(new_cat):
        with open(new_cat, encoding='utf-8') as f:
            cat2 = json.load(f)
        cat2['paper_id'] = expected
        cat2['source_id'] = expected
        cat2['asset_refs']['markdown'] = f'{expected}.md'
        cat2['asset_refs']['pdf'] = f'{expected}.pdf'
        with open(new_cat, 'w', encoding='utf-8') as f:
            json.dump(cat2, f, ensure_ascii=False, indent=2)

    fixed += 1
    print(f"  {d} -> {expected}")

print(f"\nFixed: {fixed}")
if errors:
    print(f"Errors: {len(errors)}")
    for d, e in errors[:15]:
        print(f"  {d}: {e}")
