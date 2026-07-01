import json, os, re, unicodedata

RAW = 'data/paper_raw'
LEDGER_PATH = 'data/catalog/paper_number_ledger.json'

_ILLEGAL = re.compile(r"""[\\/:*?"<>|.\s()+\-#&%!;=@~`\[\]{}',‐–—―]+""")

def sanitize(pid):
    s = _ILLEGAL.sub('_', pid)
    s = re.sub(r'\s+', '_', s.strip())
    s = s.strip('_')
    s = re.sub(r'_+', '_', s)
    return s[:80] or 'untitled'

def ascii_fold(name):
    nfkd = unicodedata.normalize('NFKD', name)
    return nfkd.encode('ascii', 'ignore').decode('ascii').lower()

def clean_author(raw):
    if not raw:
        return ''
    a = raw.strip()
    a = re.sub(r'\$[^$]*\$', '', a)
    a = re.sub(r'[0-9.,*+†‡§¶#]+', '', a)
    a = a.replace('\\', '')
    a = a.strip(' ,;.*+†‡')
    words = a.split()
    for w in reversed(words):
        w = w.strip(' ,;.()[]{}')
        if re.match(r'^[A-Za-zÀ-ÿ\']+$', w):
            return ascii_fold(w)
    return ''

with open(LEDGER_PATH, encoding='utf-8') as f:
    ledger = json.load(f)

remaining = sorted([
    d for d in os.listdir(RAW)
    if os.path.isdir(os.path.join(RAW, d)) and len(d) == 16 and d.isdigit()
])
print(f'Remaining 16-digit dirs: {len(remaining)}')
renamed = 0

for d in remaining:
    path = os.path.join(RAW, d)
    meta_path = os.path.join(path, f'{d}.metadata.json')
    if not os.path.exists(meta_path):
        print(f'  SKIP {d}: no metadata')
        continue

    with open(meta_path, encoding='utf-8') as f:
        meta = json.load(f)

    fa = meta.get('first_author', {}) or {}
    author_raw = fa.get('family', '') or fa.get('display', '') or ''
    author = clean_author(author_raw) or 'unknown'

    year = meta.get('year', '')
    if not year or not str(year).isdigit():
        year = '0000'

    zh = (meta.get('title', {}) or {}).get('short_zh', '') or ''
    if not zh:
        title_en = (meta.get('title', {}) or {}).get('original', '') or d
        words = re.findall(r'[A-Z][a-z]+', title_en[:80])
        stopwords = {'From','That','This','With','Over','Into','The','A','An','And','For','Of','In','On','To'}
        key = [w.lower() for w in words if w not in stopwords and len(w) > 2]
        zh = '_'.join(key[:4]) if key else 'untitled'

    y = str(year)
    a = author
    pid = sanitize(f'{y}_{a}_{zh}')

    new_path = os.path.join(RAW, pid)
    if os.path.exists(new_path):
        pid = sanitize(f'{y}_{a}_{zh}_{d[-4:]}')
        new_path = os.path.join(RAW, pid)
        if os.path.exists(new_path):
            print(f'  SKIP {d}: dup target {pid}')
            continue

    # Rename files inside
    for fname in list(os.listdir(path)):
        fpath = os.path.join(path, fname)
        if fname == 'images':
            continue
        if os.path.isfile(fpath):
            if fname.startswith(d + '.'):
                os.rename(fpath, os.path.join(path, fname.replace(d, pid, 1)))
            elif fname.endswith('.paper.number'):
                os.rename(fpath, os.path.join(path, f'{pid}.paper.number'))

    # Rename directory
    os.rename(path, new_path)

    # Update catalog.json
    new_cat = os.path.join(new_path, f'{pid}.catalog.json')
    if os.path.exists(new_cat):
        try:
            with open(new_cat, encoding='utf-8') as f:
                cat = json.load(f)
            cat['paper_id'] = pid
            cat['source_id'] = pid
            if 'asset_refs' in cat:
                cat['asset_refs']['markdown'] = f'{pid}.md'
                cat['asset_refs']['pdf'] = f'{pid}.pdf'
            with open(new_cat, 'w', encoding='utf-8') as f:
                json.dump(cat, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f'  WARN {d}: catalog.json invalid - {e}')

    # Update metadata source_id
    new_meta = os.path.join(new_path, f'{pid}.metadata.json')
    if os.path.exists(new_meta):
        with open(new_meta, encoding='utf-8') as f:
            meta2 = json.load(f)
        if meta2.get('source_id') == d:
            meta2['source_id'] = pid
            with open(new_meta, 'w', encoding='utf-8') as f:
                json.dump(meta2, f, ensure_ascii=False, indent=2)

    # Update ledger
    for pn, entry in list(ledger.get('items', {}).items()):
        if entry.get('folder_name') == d:
            entry['folder_name'] = pid
            entry['folder_path'] = f'data/paper_raw/{pid}'

    renamed += 1
    print(f'  {d} -> {pid}')

with open(LEDGER_PATH, 'w', encoding='utf-8') as f:
    json.dump(ledger, f, ensure_ascii=False, indent=2)

print(f'\nRenamed: {renamed}')
final = sorted([d for d in os.listdir(RAW) if os.path.isdir(os.path.join(RAW, d)) and d not in ('papers', 'quarantine')])
still_16 = [d for d in final if len(d) == 16 and d.isdigit()]
print(f'Total raw dirs: {len(final)}')
print(f'Still 16-digit: {len(still_16)}')
if still_16:
    for d in still_16:
        print(f'  {d}')
