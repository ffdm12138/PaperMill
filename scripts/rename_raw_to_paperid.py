"""
Rename all 16-digit paper_raw directories to paper_id format:
  {year}_{author_lowercase}_{ChineseShortTitle}

Reads metadata.json to derive paper_id, then renames directory + all files.
Updates catalog.json, metadata.json, .import_status.json, and ledger.
"""
import json, os, glob, re, unicodedata

RAW = 'data/paper_raw'
LEDGER_PATH = 'data/catalog/paper_number_ledger.json'

chinese_re = re.compile(r'[一-鿿]')
_ILLEGAL = re.compile(r"[\\/:*?\"<>|.\s()+\-&#%!;=@~`\[\]{}',‐–—]+")

def sanitize_paper_id(raw: str) -> str:
    s = _ILLEGAL.sub("_", raw)
    s = re.sub(r"\s+", "_", s.strip())
    s = s.strip("_")
    return s or "untitled"

def ascii_fold(name: str) -> str:
    nfkd = unicodedata.normalize("NFKD", name)
    return nfkd.encode("ascii", "ignore").decode("ascii").lower()

def make_chinese_title(meta):
    """Derive a short Chinese title from metadata."""
    zh = meta.get('title', {}).get('short_zh', '') or meta.get('title', {}).get('translated_zh', '')
    if zh:
        return sanitize_paper_id(zh)

    title_en = meta.get('title', {}).get('original', '')
    # Extract key nouns from English title
    words = re.findall(r'[A-Z][a-z]+', title_en)
    stopwords = {'From', 'That', 'This', 'With', 'Over', 'Into', 'Model', 'Study', 'Effect',
                 'Part', 'Flow', 'New', 'Method', 'Based', 'Using', 'Simulation', 'Measurement',
                 'Analysis', 'Approach', 'Results', 'Data', 'Field', 'Numerical', 'Observations'}
    key = [w for w in words if w not in stopwords and len(w) > 3]
    if key:
        return sanitize_paper_id('_'.join(key[:3]))
    return 'untitled'

def make_paper_id(year, author, zh_title):
    """Build paper_id from components."""
    a = ascii_fold(author or 'unknown')
    y = str(year) if year and str(year).isdigit() else '0000'
    z = sanitize_paper_id(zh_title)
    pid = f"{y}_{a}_{z}"
    # Final sanitization
    pid = _ILLEGAL.sub("_", pid)
    pid = re.sub(r"\s+", "_", pid.strip())
    pid = pid.strip("_")
    return pid


# ── Main ──
# Read ledger
with open(LEDGER_PATH, encoding='utf-8') as f:
    ledger = json.load(f)

raw_dirs = sorted([
    d for d in os.listdir(RAW)
    if os.path.isdir(os.path.join(RAW, d)) and d not in ('papers', 'quarantine')
])

renamed = 0
skipped = 0
errors = []

for d in raw_dirs:
    path = os.path.join(RAW, d)

    # Skip if already paper_id format (not 16-digit numeric)
    if not (len(d) == 16 and d.isdigit()):
        print(f"  SKIP (not 16-digit): {d}")
        skipped += 1
        continue

    meta_path = os.path.join(path, f'{d}.metadata.json')
    cat_path = os.path.join(path, f'{d}.catalog.json')

    if not os.path.exists(meta_path):
        errors.append(f"{d}: no metadata.json")
        skipped += 1
        continue

    with open(meta_path, encoding='utf-8') as f:
        meta = json.load(f)

    year = meta.get('year', '')
    author = meta.get('first_author', {}).get('family', '')
    zh_title = make_chinese_title(meta)

    new_pid = make_paper_id(year, author, zh_title)
    new_path = os.path.join(RAW, new_pid)

    if os.path.exists(new_path) and new_pid != d:
        errors.append(f"{d}: target {new_pid} already exists")
        skipped += 1
        continue

    if new_pid == d:
        skipped += 1
        continue

    # ── Rename files inside ──
    for fname in os.listdir(path):
        fpath = os.path.join(path, fname)
        if fname == 'images':
            continue  # moved with dir
        if os.path.isfile(fpath):
            if fname.startswith(d + '.'):
                new_name = new_pid + fname[len(d):]
                os.rename(fpath, os.path.join(path, new_name))
            elif fname == '.import_status.json':
                pass  # keep as-is
            elif fname.endswith('.paper.number'):
                # Rename to new prefix
                os.rename(fpath, os.path.join(path, f'{new_pid}.paper.number'))

    # ── Rename directory ──
    os.rename(path, new_path)

    # ── Update catalog.json ──
    new_cat = os.path.join(new_path, f'{new_pid}.catalog.json')
    if os.path.exists(new_cat):
        with open(new_cat, encoding='utf-8') as f:
            cat = json.load(f)
        cat['paper_id'] = new_pid
        cat['source_id'] = new_pid
        if 'asset_refs' in cat:
            cat['asset_refs']['markdown'] = f'{new_pid}.md'
            cat['asset_refs']['pdf'] = f'{new_pid}.pdf'
        with open(new_cat, 'w', encoding='utf-8') as f:
            json.dump(cat, f, ensure_ascii=False, indent=2)

    # ── Update metadata.json source_id (only if it was the old dir name, not numeric) ──
    new_meta = os.path.join(new_path, f'{new_pid}.metadata.json')
    if os.path.exists(new_meta):
        with open(new_meta, encoding='utf-8') as f:
            meta2 = json.load(f)
        old_sid = meta2.get('source_id', '')
        if old_sid == d:
            meta2['source_id'] = new_pid
            with open(new_meta, 'w', encoding='utf-8') as f:
                json.dump(meta2, f, ensure_ascii=False, indent=2)

    # ── Update ledger ──
    for pn, entry in list(ledger.get('items', {}).items()):
        if entry.get('folder_name') == d:
            entry['folder_name'] = new_pid
            entry['folder_path'] = f'data/paper_raw/{new_pid}'

    renamed += 1
    print(f"  {d} → {new_pid}")

# Write ledger
with open(LEDGER_PATH, 'w', encoding='utf-8') as f:
    json.dump(ledger, f, ensure_ascii=False, indent=2)

print(f"\n{'='*60}")
print(f"重命名: {renamed}")
print(f"跳过: {skipped}")
if errors:
    print(f"错误: {len(errors)}")
    for e in errors[:10]:
        print(f"  {e}")

# Final check
final = sorted([
    d for d in os.listdir(RAW)
    if os.path.isdir(os.path.join(RAW, d)) and d not in ('papers', 'quarantine')
])
still_16 = [d for d in final if len(d) == 16 and d.isdigit()]
print(f"\n最终 raw 目录: {len(final)}")
print(f"剩余 16 位数字目录: {len(still_16)}")
if still_16:
    for d in still_16[:5]:
        print(f"  {d}")
